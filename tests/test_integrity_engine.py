"""1.11 — veri butunlugu ve cokme guvenligi (motor tarafi).

Kapsam:
- dogrulayicisiz devam reddedilir (sessiz karisim yerine bastan indirme),
- aralikli indirmede sikistirilmis yanit kaynagi eler (content_encoding),
- denetim noktasi .part'i fsync eder (tamamlanan aralik kaydi diske inmis
  veriye dayanir) ve bitiste os.replace'ten once fsync eder,
- son ad ATOMIK sahiplenilir (ayni adli iki is birbirini ezemez),
- iptal/kaldirma gecici dosyalari temizler, HATA yarim dosyayi korur,
- bastan baslayan kosu eski dogrulama/uyari alanlarini sifirlar,
- yumusak 404 (HTML govde) DONE + uyari kodu olur,
- dogrulama TEK okumada, duraklatilabilir ve ilerleme bildirir.

Testler sabit uykuya dayanmaz: olay kapilari (Event) ve son tarihli
yoklamalar kullanilir.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import os
import sys
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import cdc, util
from ferdim.engine import job as job_mod
from ferdim.engine import segments
from ferdim.engine.job import (DownloadJob, JobSpec, JobState, _verify_pass)
from ferdim.i18n import LANGS, S
from ferdim.paths import AppEnv
from tests.helpers import DATA, SEG, TINY, start_server, tmp_root


def wait_until(pred, timeout=30.0, step=0.02):
    """Son tarihli yoklama (sabit uyku yok)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def wait_state(job, states, timeout=40):
    return wait_until(lambda: job.state in states, timeout, 0.05)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------- sunucular
class _Srv(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, handler):
        super().__init__(("127.0.0.1", 0), handler)
        self.payload = b""
        self.etag = ""
        self.encoding = ""
        self.content_type = "application/octet-stream"
        self.delay = 0.0
        self.served = 0
        self.no_length = False     # Content-Length yok: boyut bilinmez (-1)
        self.no_ranges = False     # Range yok sayilir: tek akis (200)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


def serve(handler) -> _Srv:
    srv = _Srv(handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class RangeHandler(BaseHTTPRequestHandler):
    """Range destekli, ayarlanabilir kaynak: govde/ETag/Content-Encoding
    testten once kurulur (devam eden iste baska icerik servis edilebilsin)."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        if urlsplit(self.path).path != "/data.bin":
            self._send(404, b"yok")
            return
        data = srv.payload
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes=") and not srv.no_ranges:
            a, b = rng[6:].split("-", 1)
            a = int(a)
            b = min(int(b), len(data) - 1)
            body = data[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
            self.send_header("Accept-Ranges", "bytes")
        else:
            body = data
            self.send_response(200)
        if srv.no_length:
            self.send_header("Connection", "close")
        else:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", srv.content_type)
        if srv.etag:
            self.send_header("ETag", srv.etag)
        if srv.encoding:
            self.send_header("Content-Encoding", srv.encoding)
        self.end_headers()
        srv.served += len(body)
        for i in range(0, len(body), 65536):
            if srv.delay:
                time.sleep(srv.delay)
            try:
                self.wfile.write(body[i:i + 65536])
            except Exception:
                return
        if srv.no_length:
            self.close_connection = True

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


SOFT_HTML = b"<!DOCTYPE html>\n<html><body>Sayfa bulunamadi</body></html>"


class Soft404Handler(BaseHTTPRequestHandler):
    """Range'siz kaynak: HTML 'yumusak 404' sayfasi, tur basliga gore."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = urlsplit(self.path).path
        ctype = {
            "/missing.iso": "text/html; charset=utf-8",   # tur HTML
            "/octet.zip": "application/octet-stream",     # tur yok: koklama
            "/page.html": "text/html; charset=utf-8",     # ad zaten HTML
            "/noext": "text/html; charset=utf-8",         # uzanti yok
        }.get(p)
        if ctype is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(SOFT_HTML)))
        self.end_headers()
        self.wfile.write(SOFT_HTML)


# ------------------------------------------------------------------ testler
class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()
        cls.env = AppEnv(root=tmp_root(cls, "ferdim-1_11-"))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def job(self, url, **kw) -> DownloadJob:
        cfg = self.env.load_config()
        cfg["max_concurrent"] = 10
        cfg["checksum_sidecar"] = kw.pop("sidecar", False)
        return DownloadJob(self.env, cfg, JobSpec(url=url, **kw))

    def run_job(self, url, timeout=40, **kw) -> DownloadJob:
        j = self.job(url, **kw)
        j.start()
        self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR), timeout),
                        f"{j.state} {j.error}")
        return j


class ValidatorTests(_Base):
    """1) Dogrulayicisiz devam: sessiz karisim yerine bastan indirme."""

    def setUp(self):
        self.rsrv = serve(RangeHandler)
        self.addCleanup(self.rsrv.shutdown)
        self.rsrv.payload = DATA
        self.rsrv.etag = '"v1"'

    def test_unit_resumable_matrix(self):
        j = self.job(f"{self.rsrv.base}/data.bin")
        j.final_filename = "data.bin"
        j.total = len(DATA)
        j.target_dir.mkdir(parents=True, exist_ok=True)
        j.part_path.write_bytes(DATA[:1000])
        meta = {"url": j.url, "etag": "", "last_modified": "",
                "ranges": [[1000, len(DATA) - 1]]}
        probe = job_mod.httpcli.ProbeResult(total=len(DATA))
        self.assertFalse(j._resumable(meta, probe),
                         "dogrulayicisiz devam reddedilmeli")
        # acik saglama varsa devam guvenlidir (degisim dogrulamada yakalanir)
        j.spec.sha256 = sha(DATA)
        self.assertTrue(j._resumable(meta, probe))
        j.spec.sha256 = ""
        # dogrulayici esitse devam edilir
        meta["etag"] = '"v1"'
        probe.etag = '"v1"'
        self.assertTrue(j._resumable(meta, probe))

    def test_resume_without_validator_restarts_from_zero(self):
        """Sunucu ne ETag ne Last-Modified veriyor: dosya degistiyse devam
        ESKI+ YENI baytlari karistirmamali; is bastan iner."""
        self.rsrv.etag = ""                     # dogrulayici YOK
        self.rsrv.delay = 0.002                 # duraklatma icin yavas akis
        j = self.job(f"{self.rsrv.base}/data.bin", segments=4,
                     filename="noval.bin")
        j.start()
        self.assertTrue(wait_until(lambda: j.done > 2 << 20, 30),
                        f"yeterli bayt inmedi: {j.done}")
        j.pause()
        self.assertEqual(j.state, JobState.PAUSED, j.error)
        self.assertTrue(j.part_path.exists(), "yarim dosya korunmali")
        other = bytes(b ^ 0x5A for b in DATA)      # ayni boyut, baska icerik
        self.rsrv.payload = other
        j.resume()
        self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                        f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.DONE, j.error)
        self.assertEqual(j.final_path.read_bytes(), other,
                         "dogrulayicisiz devam eski/yeni baytlari karistirdi")

    def test_resume_with_explicit_checksum_is_allowed_and_catches_change(self):
        """Acik saglama varsa devam edilir; degisim bitis dogrulamasinda
        yakalanir (dosya korunur, is HATA)."""
        self.rsrv.delay = 0.002                 # duraklatma yarida kalsin
        j = self.job(f"{self.rsrv.base}/data.bin", segments=4,
                     filename="chk.bin", sha256=sha(DATA))
        j.start()
        self.assertTrue(wait_until(lambda: j.done > 2 << 20, 30), j.done)
        j.pause()
        self.assertEqual(j.state, JobState.PAUSED, j.error)
        # hizli derleyicide dosya duraklatmadan once bitebiliyordu: o zaman
        # devam yalniz yayinlar (dogru sonuc DONE) ve test anlamini yitirir
        self.assertLess(j.done, len(DATA), "duraklatma indirme bittikten sonra")
        self.rsrv.payload = bytes(b ^ 0x5A for b in DATA)
        j.resume()
        self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                        f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.ERROR)
        self.assertEqual(j.error_code, "checksum_mismatch")
        self.assertTrue(j.final_path.exists(), "uyusmayan dosya korunur")


class EncodingTests(_Base):
    """2) Aralikli indirmede sikistirilmis yanit kaynagi eler."""

    def test_compressed_ranges_fail_with_content_encoding(self):
        srv = serve(RangeHandler)
        self.addCleanup(srv.shutdown)
        srv.payload = DATA[:1 << 20]
        srv.etag = '"gz"'
        srv.encoding = "gzip"
        j = self.job(f"{srv.base}/data.bin", segments=2)
        j.start()
        self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                        f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.ERROR)
        self.assertEqual(j.error_code, "content_encoding")
        self.assertIn("sikistirilmis", j.error)
        self.assertEqual(j.snapshot()["error_code"], "content_encoding")

    def test_compressed_mirror_is_dropped_primary_continues(self):
        good, good_base = start_server()
        self.addCleanup(good.shutdown)
        srv = serve(RangeHandler)
        self.addCleanup(srv.shutdown)
        srv.payload = DATA
        srv.etag = '"gz"'
        srv.encoding = "gzip"
        j = self.job(f"{good_base}/file.bin", segments=2,
                     mirrors=[f"{srv.base}/data.bin"])
        j.start()
        self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                        f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.DONE, j.error)
        self.assertEqual(j.final_path.read_bytes(), DATA)
        self.assertTrue(j._srcs[1].dead)
        self.assertEqual(j._srcs[1].code, "content_encoding")

    def test_identity_encoding_is_accepted(self):
        srv = serve(RangeHandler)
        self.addCleanup(srv.shutdown)
        srv.payload = DATA[:1 << 20]
        srv.etag = '"id"'
        srv.encoding = "identity"
        j = self.run_job(f"{srv.base}/data.bin", segments=2)
        self.assertEqual(j.state, JobState.DONE, j.error)

    def test_code_is_user_visible_in_all_languages(self):
        self.assertIn("err_content_encoding", S)
        self.assertEqual(set(S["err_content_encoding"]), set(LANGS))


class DurabilityTests(_Base):
    """3) fsync: nokta yazimindan once ve os.replace'ten once."""

    def test_checkpoint_fsyncs_part_before_meta(self):
        j = self.job(f"{self.base}/file.bin")
        j.target_dir.mkdir(parents=True, exist_ok=True)
        j._fd = os.open(j.part_path, os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(j._close_fd)
        j.total = 16
        j.done = 16                 # yazilan bayt var: fsync gerekir
        j._wrote = 16               # ... ve henuz fsync edilmedi
        j._pending = []
        events = []
        with mock.patch.object(job_mod.os, "fsync",
                               lambda fd: events.append(("fsync", fd))), \
                mock.patch.object(job_mod.util, "atomic_write_json",
                                  lambda p, o: events.append(("meta", str(p)))):
            j._checkpoint(force=True)
        self.assertEqual([e[0] for e in events], ["fsync", "meta"],
                         "tamamlanan aralik kaydi fsync'ten once yazilmamali")

    def test_finalize_fsyncs_before_replace(self):
        j = self.job(f"{self.base}/tiny.bin")
        events = []

        real_fsync = os.fsync
        real_replace = os.replace

        def fsync(fd):
            events.append(("fsync", fd))
            return real_fsync(fd)

        def replace(src, dst):
            events.append(("replace", str(src), str(dst)))
            return real_replace(src, dst)

        with mock.patch.object(job_mod.os, "fsync", fsync), \
                mock.patch.object(job_mod.os, "replace", replace):
            j.start()
            self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                            f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.DONE, j.error)
        part = str(j.part_path)
        idx_replace = next(i for i, e in enumerate(events)
                           if e[0] == "replace" and e[1] == part)
        self.assertTrue(any(e[0] == "fsync" for e in events[:idx_replace]),
                        f"os.replace'ten once fsync yok: {events}")


class ReservationTests(_Base):
    """7) Son ad atomik sahiplenilir; ayni adli iki is cakismaz."""

    def test_unique_path_claims_atomically(self):
        root = tmp_root(self, "ferdim-claim-")
        root.mkdir(parents=True, exist_ok=True)
        out: list[Path] = []
        lock = threading.Lock()

        def worker():
            got = [util.unique_path(root, "ayni.bin") for _ in range(4)]
            with lock:
                out.extend(got)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(out), 32)
        self.assertEqual(len({str(p) for p in out}), 32,
                         "ayni ad iki ise verildi (kontrol-sonra-kullan)")
        for p in out:
            self.assertTrue(p.exists(), "sahiplenilen ad olusturulmali")
            p.unlink()

    def test_two_jobs_with_same_name_do_not_overwrite(self):
        j1 = self.run_job(f"{self.base}/file.bin", segments=2,
                          filename="ayni.bin")
        j2 = self.run_job(f"{self.base}/tiny.bin", segments=2,
                          filename="ayni.bin")
        self.assertEqual(j1.state, JobState.DONE, j1.error)
        self.assertEqual(j2.state, JobState.DONE, j2.error)
        self.assertNotEqual(j1.final_path, j2.final_path)
        self.assertEqual(j1.final_path.read_bytes(), DATA)
        self.assertEqual(j2.final_path.read_bytes(), TINY,
                         "ayni adli ikinci is birincinin dosyasini ezdi")

    def test_failed_finalize_drops_placeholder(self):
        """Ad sahiplenildikten sonra tasima basarisiz olursa bos yer tutucu
        kalmaz (yarim ad dosyasi kullaniciya gorunmez)."""
        first = self.run_job(f"{self.base}/tiny.bin", filename="bos.bin")
        self.assertEqual(first.state, JobState.DONE, first.error)
        self.assertEqual(first.final_path.read_bytes(), TINY)
        j = self.job(f"{self.base}/tiny.bin", filename="bos.bin")
        with mock.patch.object(job_mod.os, "replace",
                               side_effect=OSError("yok")), \
                mock.patch("shutil.move", side_effect=OSError("yok")):
            j.start()
            self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                            f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.ERROR)
        empties = [p.name for p in j.target_dir.iterdir() if p.stat().st_size == 0]
        self.assertEqual(empties, [], f"bos yer tutucu kaldi: {empties}")
        self.assertEqual(first.final_path.read_bytes(), TINY,
                         "basarisiz is, onceki gercek dosyaya dokunmamali")


class CleanupTests(_Base):
    """8) Gecici dosyalar: iptal temizler, HATA yarim dosyayi korur."""

    def test_cancel_removes_part_and_segs(self):
        j = self.job(f"{self.base}/slow.bin", segments=2,
                     filename="temiz.bin")
        j.start()
        self.assertTrue(wait_until(lambda: j.state == JobState.DOWNLOADING,
                                   20), j.state)
        segs = segments.temp_dir(j.meta_path)
        segs.mkdir(parents=True, exist_ok=True)
        (segs / "000000.seg").write_bytes(b"x")
        j.pause()
        self.assertEqual(j.state, JobState.PAUSED, j.error)
        self.assertTrue(j.part_path.exists())
        j.cancel()
        self.assertEqual(j.state, JobState.CANCELED)
        self.assertFalse(j.part_path.exists(), "iptal yarim dosyayi silmeli")
        self.assertFalse(j.meta_path.exists(), "iptal noktayi silmeli")
        self.assertFalse(segs.exists(), "iptal .segs klasorunu silmeli")

    def test_cancel_drops_reserved_placeholder(self):
        j = self.job(f"{self.base}/file.bin", filename="yer.bin")
        j.target_dir.mkdir(parents=True, exist_ok=True)
        j._reserved = util.unique_path(j.target_dir, "yer.bin")
        self.assertTrue(j._reserved.exists())
        j.cancel()
        self.assertFalse((j.target_dir / "yer.bin").exists(),
                         "iptal edilen is ad yer tutucusunu birakti")

    def test_resumable_error_keeps_part(self):
        """HATA (akis durdu) yarim dosyayi KORUR: resume/restart yarim
        dosyadan surdurur."""
        j = self.job(f"{self.base}/trickle.bin", segments=1,
                     filename="kesik.bin")
        j.cfg["stall_timeout"] = 5
        j.cfg["retries"] = 0
        j.start()
        self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR), 40),
                        f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.ERROR)
        self.assertTrue(j.part_path.exists(), "yeniden baslatilabilir .part silindi")
        self.assertTrue(j.meta_path.exists(), "devam noktasi silindi")
        j.cancel()
        self.assertFalse(j.part_path.exists(), "iptal yarim dosyayi silmeli")

    def test_prepare_failure_does_not_leak_part_fd(self):
        """Hazirlik (merkle bildirimi) patlarsa acik .part taniticisi sizmaz."""
        j = self.job(f"{self.base}/tiny.bin", filename="sizinti.bin",
                     merkle=str(tmp_root(self, "ferdim-yok-") / "yok.merkle"))
        part = j.part_path.name

        def part_fds():
            found = []
            for name in os.listdir("/proc/self/fd"):
                try:
                    if os.path.basename(os.readlink(f"/proc/self/fd/{name}")) == part:
                        found.append(name)
                except OSError:
                    pass
            return found

        for _ in range(3):
            j.start()
            self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                            f"{j.state} {j.error}")
            self.assertEqual(j.state, JobState.ERROR)
            self.assertEqual(j.error_code, "merkle_manifest")
        self.assertEqual(part_fds(), [], "hazirlik hatasi fd sizintisi birakti")
        j.cancel()


class ResetFieldsTests(_Base):
    """9) Bastan baslayan kosu eski dogrulama/uyari alanlarini siler."""

    def test_fresh_start_clears_stale_verification_fields(self):
        j = self.job(f"{self.base}/slow.bin", segments=2,
                     filename="sifirla.bin")
        j.start()
        self.assertTrue(wait_until(lambda: j.done > 100_000, 20), j.done)
        j.pause()
        self.assertEqual(j.state, JobState.PAUSED, j.error)
        j.extra.update({"checksum": "warn", "checksum_note": "md5",
                        "merkle": "warn", "merkle_note": "size",
                        "verified": ["md5"], "peer_bytes": 4096,
                        "warning": "soft_404"})
        # kaynak degisti (ETag farki): devam reddedilir -> bastan indirme
        j.url = f"{self.base}/file2.bin"
        j.total = -1
        j.resume()
        self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                        f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.DONE, j.error)
        snap = j.snapshot()
        self.assertEqual(snap["checksum"], "", "eski saglama izi kaldi")
        self.assertEqual(snap["merkle"], "", "eski merkle izi kaldi")
        self.assertEqual(snap["verified"], [], "eski dogrulama listesi kaldi")
        self.assertEqual(snap["warning"], "", "eski uyari kaldi")
        self.assertNotIn("peer_bytes", j.extra)
        self.assertNotIn("soft_404", snap["warning_codes"])


class Soft404Tests(_Base):
    """10) HTML govde: DONE + uyari (is hataya cevrilmez)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.s404 = serve(Soft404Handler)

    @classmethod
    def tearDownClass(cls):
        cls.s404.shutdown()
        super().tearDownClass()

    def test_soft_404_by_content_type(self):
        j = self.run_job(f"{self.s404.base}/missing.iso")
        self.assertEqual(j.state, JobState.DONE, j.error)
        self.assertEqual(j.final_path.read_bytes(), SOFT_HTML)
        snap = j.snapshot()
        self.assertEqual(snap["warning"], "soft_404")
        self.assertIn("soft_404", snap["warning_codes"])

    def test_soft_404_by_sniffed_body(self):
        j = self.run_job(f"{self.s404.base}/octet.zip")
        self.assertEqual(j.state, JobState.DONE, j.error)
        self.assertEqual(j.snapshot()["warning"], "soft_404")

    def test_html_name_or_no_extension_is_not_flagged(self):
        for name in ("page.html", "noext"):
            with self.subTest(name=name):
                j = self.run_job(f"{self.s404.base}/{name}")
                self.assertEqual(j.state, JobState.DONE, j.error)
                self.assertEqual(j.snapshot()["warning"], "",
                                 f"{name}: yumusak 404 yanlis tetiklendi")

    def test_real_file_is_not_flagged(self):
        j = self.run_job(f"{self.base}/tiny.bin")
        self.assertEqual(j.state, JobState.DONE, j.error)
        self.assertEqual(j.snapshot()["warning"], "")


class VerifyPassTests(_Base):
    """11) TEK okumada dogrulama, ilerleme ve duraklatilabilirlik."""

    def _manifest(self, payload: bytes, root: Path):
        src = root / "kaynak.bin"
        src.write_bytes(payload)
        man = cdc.make_manifest(src)
        path = root / "kaynak.ferdim-merkle"
        path.write_text(man.dumps(), "utf-8")
        return man, path

    def test_verify_pass_matches_cdc_verify_file(self):
        root = tmp_root(self, "ferdim-pass-")
        root.mkdir(parents=True, exist_ok=True)
        man, _ = self._manifest(DATA, root)
        good = root / "iyi.bin"
        good.write_bytes(DATA)
        self.assertEqual(_verify_pass(good, {"sha256": sha(DATA)}, man),
                         ({"sha256": sha(DATA)}, (True, [])))
        bad = root / "bozuk.bin"
        broken = bytearray(DATA)
        off, ln, _hx = man.chunks[1]
        broken[off + 3] ^= 0xFF
        bad.write_bytes(bytes(broken))
        got, (size_ok, chunks) = _verify_pass(bad, {}, man)
        self.assertEqual(got, {})
        self.assertTrue(size_ok)
        self.assertEqual((size_ok, chunks), cdc.verify_file(bad, man),
                         "tek gecisli dogrulama cdc ile ayni sonucu vermeli")
        # boyut uyusmazligi: okuma yok, cdc ile ayni karar
        short = root / "kisa.bin"
        short.write_bytes(DATA[:1000])
        self.assertEqual(_verify_pass(short, {}, man)[1],
                         cdc.verify_file(short, man))

    def test_single_pass_with_all_digests_and_merkle(self):
        root = tmp_root(self, "ferdim-pass2-")
        man, man_path = self._manifest(DATA, root)
        calls = {"digests": [], "verify_file": 0}
        real_digests = job_mod.util.file_digests
        real_verify = job_mod.cdc.verify_file

        def spy_digests(path, algos, **kw):
            calls["digests"].append(dict(algos) if isinstance(algos, dict)
                                    else list(algos))
            return real_digests(path, algos, **kw)

        def spy_verify(*a, **kw):
            calls["verify_file"] += 1
            return real_verify(*a, **kw)

        progress: list[int] = []
        orig_progress = DownloadJob._verify_progress

        def spy_progress(self, done):
            progress.append(int(done))
            orig_progress(self, done)

        with mock.patch.object(job_mod.util, "file_digests", spy_digests), \
                mock.patch.object(job_mod.cdc, "verify_file", spy_verify), \
                mock.patch.object(DownloadJob, "_verify_progress",
                                  spy_progress):
            j = self.job(f"{self.base}/file.bin", segments=4,
                         merkle=str(man_path), sha256=sha(DATA),
                         md5=hashlib.md5(DATA).hexdigest())
            j.start()
            self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                            f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.DONE, j.error)
        snap = j.snapshot()
        self.assertEqual(snap["checksum"], "ok")
        self.assertEqual(snap["merkle"], "ok")
        self.assertEqual(sorted(snap["verified"]), ["md5", "merkle", "sha256"])
        # butun ozetler TEK cagrida (tek okuma)
        self.assertEqual(len(calls["digests"]), 1, calls["digests"])
        self.assertEqual(sorted(calls["digests"][0]), ["md5", "sha256"])
        # merkle ayni okumada: bitis dogrulamasi cdc'yi yeniden cagirmaz
        self.assertEqual(calls["verify_file"], 1,
                         "merkle ikinci kez okundu (onarim turu disinda)")
        self.assertTrue(progress, "dogrulama ilerlemesi bildirilmedi")
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(progress[-1], len(DATA))
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(j.snapshot()["verify_total"], 0,
                         "ilerleme alanlari bitisten sonra kalmamali")

    def test_pause_during_verification_cuts_the_pass_short(self):
        progress: list[int] = []
        orig = DownloadJob._verify_progress
        gate = threading.Event()

        def do_pause(job):
            job.pause()

        def spy(self, done):
            progress.append(int(done))
            orig(self, done)
            if not gate.is_set():
                gate.set()
                threading.Thread(target=do_pause, args=(self,),
                                 daemon=True).start()
                end = time.monotonic() + 10
                while not self._stop.is_set() and time.monotonic() < end:
                    time.sleep(0.005)

        j = self.job(f"{self.base}/file.bin", segments=4,
                     filename="duraklat-dogrula.bin", sha256=sha(DATA))
        with mock.patch.object(DownloadJob, "_verify_progress", spy):
            j.start()
            self.assertTrue(wait_state(j, (JobState.PAUSED, JobState.DONE,
                                          JobState.ERROR), 40),
                            f"{j.state} {j.error}")
            self.assertTrue(gate.wait(10), "dogrulama baslamadi")
        t0 = time.monotonic()
        while time.monotonic() - t0 < 20 and j.state != JobState.PAUSED:
            time.sleep(0.02)
        self.assertEqual(j.state, JobState.PAUSED, j.error)
        self.assertLess(progress[-1], len(DATA),
                        "duraklatma dogrulamayi kesmedi (tam okuma)")
        self.assertNotEqual(j.state, JobState.DONE)
        self.assertEqual(j.snapshot()["checksum"], "",
                         "kesilen dogrulama karar yazmamali")
        j.cancel()


class RemoveCleanupTests(unittest.TestCase):
    def test_remove_paused_job_drops_part_and_meta(self):
        import tempfile
        from pathlib import Path
        from ferdim.paths import AppEnv
        from ferdim.engine.queue import QueueManager
        from ferdim.engine.job import JobSpec
        from ferdim.paths import DEFAULTS
        with tempfile.TemporaryDirectory() as d:
            env = AppEnv(root=Path(d))
            qm = QueueManager(env, dict(DEFAULTS))
            try:
                job = qm.add(JobSpec(url="http://127.0.0.1:9/x.bin"), paused=True)
                job.part_path.parent.mkdir(parents=True, exist_ok=True)
                job.part_path.write_bytes(b"x" * 10)
                job.meta_path.write_text("{}")
                self.assertTrue(qm.remove(job.id))
                self.assertFalse(job.part_path.exists())
                self.assertFalse(job.meta_path.exists())
            finally:
                qm.shutdown()


# ---------------------------------------------------------------- inceleme
class FreshStartTruncateTests(_Base):
    """H1: bastan baslayan kosu .part'i kirpar.

    Bilinmeyen boyutta (total = -1) `ftruncate(total)` yoktur: eski kosunun
    kuyrugu kirpilmazsa yeni icerigin ardinda kalir ve is yanlislikla DONE
    olur."""

    def test_unknown_size_fresh_start_truncates_stale_tail(self):
        srv = serve(RangeHandler)
        self.addCleanup(srv.shutdown)
        body = DATA[:300_000]
        srv.payload = body
        srv.no_length = True       # Content-Length yok: boyut bilinmez (-1)
        srv.no_ranges = True       # tek akis
        j = self.job(f"{srv.base}/data.bin", filename="bilinmeyen.bin")
        j.target_dir.mkdir(parents=True, exist_ok=True)
        j.part_path.write_bytes(b"X" * (1 << 20))     # eski, uzun kuyruk
        j.start()
        self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                        f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.DONE, j.error)
        self.assertEqual(j.final_path.read_bytes(), body,
                         "eski .part kuyrugu yeni dosyaya kaldi")
        self.assertEqual(j.final_path.stat().st_size, len(body))


class FinalizeDurabilityTests(_Base):
    """H2: yayinlanan ad diske iner; yalniz EXDEV kopyalamaya duser."""

    def test_dir_fsync_after_rename(self):
        j = self.job(f"{self.base}/tiny.bin", filename="ad-fsync.bin")
        events = []
        real_replace = os.replace

        def replace(src, dst):
            events.append(("replace", str(src), str(dst)))
            return real_replace(src, dst)

        with mock.patch.object(job_mod.os, "replace", replace), \
                mock.patch.object(job_mod, "fsync_dir",
                                  lambda p: events.append(("fsync_dir", str(p)))):
            j.start()
            self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                            f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.DONE, j.error)
        part = str(j.part_path)
        idx = next(i for i, e in enumerate(events)
                   if e[0] == "replace" and e[1] == part)
        self.assertTrue(any(e[0] == "fsync_dir" for e in events[idx + 1:]),
                        f"replace sonrasi dizin fsync yok: {events}")

    def test_non_exdev_rename_error_is_not_hidden(self):
        j = self.job(f"{self.base}/tiny.bin", filename="exdev.bin")
        real_replace = os.replace

        def replace(src, dst):
            if str(src) == str(j.part_path):
                raise OSError(errno.EACCES, "yok")
            return real_replace(src, dst)

        with mock.patch.object(job_mod.os, "replace", replace), \
                mock.patch("shutil.move") as moved:
            j.start()
            self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                            f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.ERROR)
        self.assertEqual(j.error_code, "disk", j.error)
        moved.assert_not_called()

    def test_exdev_fallback_fsyncs_the_copy(self):
        j = self.job(f"{self.base}/tiny.bin", filename="exdev2.bin")
        synced = []
        real_fsync = os.fsync
        real_replace = os.replace

        def replace(src, dst):
            if str(src) == str(j.part_path):
                raise OSError(errno.EXDEV, "baska fs")
            return real_replace(src, dst)

        def fsync(fd):
            synced.append(os.readlink(f"/proc/self/fd/{fd}"))
            return real_fsync(fd)

        with mock.patch.object(job_mod.os, "replace", replace), \
                mock.patch.object(job_mod.os, "fsync", fsync):
            j.start()
            self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                            f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.DONE, j.error)
        self.assertEqual(j.final_path.read_bytes(), TINY)
        self.assertTrue(any(n.endswith(j.final_path.name) for n in synced),
                        f"kopya fsync edilmedi: {synced}")


class CheckpointSyncTests(_Base):
    """M2: checkpoint fsync'i kilidin DISINDA ve yalniz yeni bayt varsa."""

    def _job_with_fd(self, size=64):
        j = self.job(f"{self.base}/file.bin")
        j.target_dir.mkdir(parents=True, exist_ok=True)
        j._fd = os.open(j.part_path, os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(j._close_fd)
        j.total = size
        j._pending = [(0, size - 1)]
        j._inflight = {1: (16, size - 1)}
        j.done = 16
        j._wrote = 16          # 16 bayt yazildi, henuz fsync edilmedi
        return j

    def test_fsync_runs_without_the_lock(self):
        j = self._job_with_fd()
        entered, release = threading.Event(), threading.Event()

        def slow_fsync(fd):
            entered.set()
            release.wait(10)

        with mock.patch.object(job_mod.os, "fsync", slow_fsync):
            th = threading.Thread(target=j._checkpoint, kwargs={"force": True})
            th.start()
            try:
                self.assertTrue(entered.wait(10), "fsync calismadi")
                free = j._lock.acquire(timeout=2.0)
                if free:
                    j._lock.release()
                self.assertTrue(free, "fsync kilit tutulurken calisiyor")
            finally:
                release.set()
                th.join(10)
        self.assertFalse(th.is_alive())
        self.assertTrue(j.meta_path.exists())

    def test_no_fsync_when_nothing_new_was_written(self):
        j = self._job_with_fd()
        calls = []
        part_ino = os.fstat(j._fd).st_ino
        with mock.patch.object(job_mod.os, "fsync",
                               lambda fd: calls.append(os.fstat(fd).st_ino)):
            j._checkpoint(force=True)      # ilerleme var -> fsync
            j._checkpoint(force=True)      # yeni bayt yok -> fsync yok
        self.assertEqual(calls.count(part_ino), 1, calls)
        self.assertEqual(len(util.read_json(j.meta_path)["ranges"]), 2)

    def test_fsync_error_keeps_the_previous_checkpoint(self):
        j = self._job_with_fd()
        j._checkpoint(force=True)
        before = j.meta_path.read_text("utf-8")
        j.done = 32
        j._wrote = 32               # yeni baytlar: fsync gerekir (ve patlar)
        j._pending = [(0, 31)]
        real_fsync = os.fsync
        part_ino = os.fstat(j._fd).st_ino

        def fsync(fd):
            if os.fstat(fd).st_ino == part_ino:
                raise OSError(errno.EIO, "yok")
            return real_fsync(fd)

        with mock.patch.object(job_mod.os, "fsync", fsync):
            j._checkpoint(force=True)
        self.assertEqual(j.meta_path.read_text("utf-8"), before,
                         "fsync hatasinda nokta yazilmamali")

    def test_fd_closed_during_fsync_still_writes_checkpoint(self):
        """HATA yolu: izleyici .part fd'sini fsync sirasinda kapatsa da
        nokta yazilir (eskiden dusuyordu; ERROR isi devam noktasiz kaliyordu)."""
        j = self._job_with_fd()
        real_fsync = os.fsync

        def fsync(fd):
            j._close_fd()
            return real_fsync(fd)

        with mock.patch.object(job_mod.os, "fsync", fsync):
            j._checkpoint(force=True)
        self.assertTrue(j.meta_path.exists(), "devam noktasi yazilmadi")
        self.assertEqual(util.read_json(j.meta_path)["done"], 16)


class EncodingTokenTests(_Base):
    """M3: Content-Encoding token listesi; sikistirma reddedilir."""

    def test_identity_token_matrix(self):
        for val in ("", " ", "identity", "none", "identity, identity",
                    "NONE,Identity", " Identity , none "):
            self.assertTrue(job_mod._identity_encoding(val), repr(val))
        for val in ("gzip", "br", "deflate", "identity, gzip", "gzip, br"):
            self.assertFalse(job_mod._identity_encoding(val), repr(val))

    def test_none_encoding_is_accepted(self):
        srv = serve(RangeHandler)
        self.addCleanup(srv.shutdown)
        srv.payload = DATA[:1 << 20]
        srv.etag = '"none"'
        srv.encoding = "none"
        j = self.run_job(f"{srv.base}/data.bin", segments=2)
        self.assertEqual(j.state, JobState.DONE, j.error)
        self.assertEqual(j.final_path.read_bytes(), srv.payload)

    def test_unranged_200_with_gzip_fails_with_content_encoding(self):
        srv = serve(RangeHandler)
        self.addCleanup(srv.shutdown)
        srv.payload = DATA[:200_000]
        srv.no_ranges = True           # 200 + tam govde (tek akis)
        srv.encoding = "gzip"
        j = self.job(f"{srv.base}/data.bin")
        j.start()
        self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                        f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.ERROR)
        self.assertEqual(j.error_code, "content_encoding")


class ReservationRecoveryTests(_Base):
    """L1: yayinlanmis dosya yer tutucu sanilip silinmez; cokme kaydi kurtarilir."""

    def test_published_file_survives_drop_reservation(self):
        j = self.run_job(f"{self.base}/tiny.bin", filename="yayin.bin")
        self.assertEqual(j.state, JobState.DONE, j.error)
        with j._lock:
            j._reserved = j.final_path        # yayinlanmis dosyaya isaret
        j._drop_reservation()
        self.assertTrue(j.final_path.exists(), "yayinlanmis dosya silindi")

    def test_stale_placeholder_is_removed_on_prepare(self):
        j = self.job(f"{self.base}/tiny.bin", filename="artik.bin")
        j.target_dir.mkdir(parents=True, exist_ok=True)
        ph = util.unique_path(j.target_dir, "eski.bin")     # bos yer tutucu
        st = ph.stat()
        util.atomic_write_json(j.meta_path, {
            "url": j.url, "etag": "", "last_modified": "",
            "ranges": [[0, len(TINY) - 1]], "done": 0,
            "reserved": [ph.name, st.st_dev, st.st_ino]})
        j.start()
        self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                        f"{j.state} {j.error}")
        self.assertFalse(ph.exists(), "bos yer tutucu silinmedi")

    def test_non_empty_reserved_file_is_kept(self):
        j = self.job(f"{self.base}/tiny.bin", filename="dolu.bin")
        j.target_dir.mkdir(parents=True, exist_ok=True)
        keep = j.target_dir / "dolu.bin"
        keep.write_bytes(b"gercek veri")
        st = keep.stat()
        util.atomic_write_json(j.meta_path, {
            "url": j.url, "etag": "", "last_modified": "",
            "ranges": [[0, len(TINY) - 1]], "done": 0,
            "reserved": [keep.name, st.st_dev, st.st_ino]})
        j.start()
        self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                        f"{j.state} {j.error}")
        self.assertEqual(keep.read_bytes(), b"gercek veri",
                         "dolu dosya yer tutucu sanilip silindi")


class PublishOnlyTests(_Base):
    """L4: tam inmis ama yayinlanmamis .part yeniden indirilmez."""

    def _job_with_complete_part(self, srv, filename="hazir.bin", etag='"v1"'):
        j = self.job(f"{srv.base}/data.bin", filename=filename)
        j.target_dir.mkdir(parents=True, exist_ok=True)
        j.part_path.write_bytes(DATA)
        util.atomic_write_json(j.meta_path, {
            "url": j.url, "etag": etag, "last_modified": "",
            "ranges": [], "done": len(DATA), "total": len(DATA)})
        return j

    def test_complete_part_is_published_without_redownload(self):
        srv = serve(RangeHandler)
        self.addCleanup(srv.shutdown)
        srv.payload = DATA
        srv.etag = '"v1"'
        j = self._job_with_complete_part(srv)
        srv.served = 0
        j.start()
        self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                        f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.DONE, j.error)
        self.assertEqual(j.final_path.read_bytes(), DATA)
        self.assertLess(srv.served, len(DATA), "tamamlanmis dosya yeniden indi")

    def test_complete_part_without_validator_is_redownloaded(self):
        srv = serve(RangeHandler)
        self.addCleanup(srv.shutdown)
        srv.payload = DATA
        srv.etag = ""
        j = self._job_with_complete_part(srv, etag="")
        srv.served = 0
        j.start()
        self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR)),
                        f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.DONE, j.error)
        self.assertEqual(j.final_path.read_bytes(), DATA)
        self.assertGreaterEqual(srv.served, len(DATA),
                                "dogrulayicisiz tam dosya guvenilip yayinlandi")


class WarningSurfaceTests(unittest.TestCase):
    """L5: yumusak 404 uyarisi GTK rozetinde ve `ferdim list` ciktisinda."""

    def test_badge_shows_soft_404(self):
        from ferdim.gui import present
        badge = present.integrity_badge({"warning": "soft_404"}, "en")
        self.assertIsNotNone(badge)
        _symbol, tip, css = badge
        self.assertEqual(css, "warning")
        self.assertIn(S["warn_soft_404"]["en"], tip)

    def test_cli_list_shows_soft_404_warning(self):
        from ferdim import cli

        class Fake:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def request(self, cmd, **kw):
                return {"jobs": [{"id": "abc123", "state": "done", "done": 10,
                                  "size": 10, "speed": 0, "filename": "x.iso",
                                  "warning": "soft_404"}]}

        args = types.SimpleNamespace(json=False)
        out = io.StringIO()
        with mock.patch.object(cli, "_client", lambda env: Fake()), \
                mock.patch.object(
                    cli, "_",
                    lambda key, **kw: (S.get(key) or {}).get("en", key)), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_list(args, None), 0)
        text = out.getvalue()
        self.assertIn("x.iso", text)
        self.assertIn(S["warn_soft_404"]["en"], text)


class SegmentsDurabilityTests(_Base):
    """M4: segment dosyalari ve birlesmis cikti, nokta ilerlemeden fsync edilir."""

    def test_every_meta_write_follows_an_output_fsync(self):
        events = []
        real_fsync = os.fsync

        def fsync(fd):
            try:
                name = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                name = ""
            events.append(("fsync", name))
            return real_fsync(fd)

        real_write = util.atomic_write_json

        def write_meta(path, obj):
            events.append(("meta", str(path)))
            return real_write(path, obj)

        with mock.patch.object(segments.os, "fsync", fsync), \
                mock.patch.object(segments.util, "atomic_write_json", write_meta):
            j = self.job(f"{self.base}/master.m3u8", segments=4,
                         filename="segment-disk.ts")
            j.start()
            self.assertTrue(wait_state(j, (JobState.DONE, JobState.ERROR), 60),
                            f"{j.state} {j.error}")
        self.assertEqual(j.state, JobState.DONE, j.error)
        self.assertEqual(j.final_path.read_bytes(), b"".join(SEG))
        # 1) segment gecici dosyasi ad degistirmeden once fsync edilir
        self.assertTrue(any(kind == "fsync" and name.endswith(".part")
                            and ".segs/" in name for kind, name in events),
                        "segment gecici dosyasi fsync edilmedi")
        # 2) is noktasi, birlesmis ciktinin fsync'inden once hic yazilmaz
        part = j.part_path.name
        meta = str(j.meta_path)
        seen_out = False
        wrote = 0
        for kind, name in events:
            if kind == "fsync" and name and Path(name).name == part:
                seen_out = True
            elif kind == "meta" and name == meta:
                wrote += 1
                self.assertTrue(seen_out,
                                f"meta cikti fsync'inden once yazildi: {events}")
        self.assertGreater(wrote, 0, "hic nokta yazilmadi")


if __name__ == "__main__":
    unittest.main(verbosity=2)
