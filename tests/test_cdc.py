"""Icerik-tanimli parcalama (cdc) + Merkle: birim, motor ve onarim testleri.

- Parcalama deterministik ve icerige bagli olmali: basina bayt eklenen
  dosyada parcalarin cogu ayni kalir.
- Motor bozuk parcalari YALNIZCA o bayt araliklariyla yeniden indirir;
  onarim MERKLE_REPAIR_MAX turla sinirlidir.
- `update` yolu yerel dosyadaki eslesen parcalari korur.
"""
from __future__ import annotations

import hashlib
import json
import mmap
import os
import sys
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import cdc
from ferdim.engine import httpcli, job as job_mod
from ferdim.engine.job import MERKLE_REPAIR_MAX, JobSpec, JobState
from ferdim.paths import AppEnv
from tests.helpers import tmp_root

# sabit veri: parca sinirlari her turda ayni olsun (os.urandom degil)
PAYLOAD = hashlib.shake_256(b"ferdim-cdc-test").digest((4 << 20) + 7777)
SMALL = dict(min_size=4096, avg_size=16384, max_size=65536)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wait_state(job, states, timeout=60):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if job.state in states:
            return True
        time.sleep(0.05)
    return False


def chunk_bytes(buf: bytes, **kw) -> list[tuple[int, int, str]]:
    return [(s, e - s, sha(buf[s:e]))
            for s, e in cdc.chunk_ranges(buf, **kw)]


def phases(ranges, total: int):
    """Istekleri indirme/onarim olarak ayirir: ilk faz tam `total` bayt
    ister (her bayt bir kez), sonrasi onarimdir."""
    acc, i = 0, 0
    while i < len(ranges) and acc < total:
        acc += ranges[i][1] - ranges[i][0] + 1
        i += 1
    return ranges[:i], ranges[i:]


class ChunkerTests(unittest.TestCase):
    def setUp(self):
        # sabit, TEKRARSIZ veri (periyodik dolgu kayma testini bozardi)
        self.buf = hashlib.shake_128(b"cdc-test").digest(768 << 10)

    def test_01_deterministic_and_bounded(self):
        a = list(cdc.chunk_ranges(self.buf, **SMALL))
        b = list(cdc.chunk_ranges(self.buf, **SMALL))
        self.assertEqual(a, b)
        self.assertEqual(a[0][0], 0)
        self.assertEqual(a[-1][1], len(self.buf))
        for (s, e), nxt in zip(a, a[1:]):
            self.assertEqual(e, nxt[0], "parcalar bitisik olmali")
        for s, e in a[:-1]:
            self.assertGreaterEqual(e - s, SMALL["min_size"])
            self.assertLessEqual(e - s, SMALL["max_size"])

    def test_02_boundary_shift_robust(self):
        """Basina bayt eklemek sonraki parcalarin cogunu degistirmez."""
        shifts = (1, 7, 137, 4096)
        base = [h for _, _, h in chunk_bytes(self.buf, **SMALL)]
        for n in shifts:
            with self.subTest(n=n):
                moved = self.buf[:5000] + b"\xab" * n + self.buf[5000:]
                got = [h for _, _, h in chunk_bytes(moved, **SMALL)]
                self.assertGreaterEqual(len(set(base) & set(got)),
                                        len(base) - 1,
                                        "kayma sonrasi parcalar bozulmamali")

    def test_03_merkle_root(self):
        h = [sha(b"a"), sha(b"b"), sha(b"c")]
        self.assertEqual(cdc.merkle_root(h), cdc.merkle_root(list(h)))
        self.assertNotEqual(cdc.merkle_root(h), cdc.merkle_root(h[::-1]))
        self.assertNotEqual(cdc.merkle_root(h), cdc.merkle_root(h[:2]))
        self.assertEqual(len(cdc.merkle_root([])), 64)

    def test_04_manifest_roundtrip(self):
        # bildirim bicimi varsayilan parametreleri tasir (parse bunlari
        # dogrular); kucuk parametreler yalnizca uretici API'sinde kalir
        root = cdc.merkle_root([h for _, _, h in chunk_bytes(self.buf)])
        man = cdc.Manifest.of(chunk_bytes(self.buf))
        self.assertEqual(man.root, root)
        self.assertEqual(man.size, len(self.buf))
        again = cdc.Manifest.parse(man.dumps())
        self.assertEqual(again, man)
        off, ln = man.offset_of(70000)
        self.assertTrue(off <= 70000 < off + ln)

    def test_05_manifest_rejects_bad_input(self):
        man = cdc.Manifest.of(chunk_bytes(self.buf))
        bad = [
            "bozuk json",
            man.dumps().replace('"v":1', '"v":2'),
            man.dumps().replace(cdc.ALGO, "md5"),
            man.dumps().replace(f'"size":{man.size}', '"size":1'),
        ]
        chunks = [list(c) for c in man.chunks]
        chunks[0][2] = "0" * 64            # kok artik uyusmuyor
        bad.append(json.dumps({"v": 1, "algo": cdc.ALGO, "size": man.size,
                               "root": man.root, "chunks": chunks}))
        for text in bad:
            with self.subTest(text=text[:24]):
                with self.assertRaises(ValueError):
                    cdc.Manifest.parse(text)

    def test_06_verify_detects_corrupted_ranges(self):
        root = tmp_root(self)
        p = root / "f.bin"
        p.write_bytes(self.buf)
        man = cdc.make_manifest(p, **SMALL)
        self.assertEqual(cdc.verify_file(p, man), (True, []))
        target = man.chunks[2]
        raw = bytearray(self.buf)
        raw[target[0] + 3] ^= 0xFF
        p.write_bytes(bytes(raw))
        size_ok, bad = cdc.verify_file(p, man)
        self.assertTrue(size_ok)
        self.assertEqual(bad, [(target[0], target[1])])
        p.write_bytes(self.buf + b"x")
        self.assertEqual(cdc.verify_file(p, man), (False, []))

    def test_07_deeply_nested_json_is_a_value_error(self):
        """Fuzz bulgusu: derin ic ice JSON'da json.loads RecursionError
        sizdiriyordu; Manifest.parse sozlesmesi tek tur (ValueError)."""
        deep = "[" * 5000 + "]" * 5000
        text = ('{"v":1,"algo":"fastcdc-sha256","size":0,"root":'
                + deep + "}")
        with self.assertRaises(ValueError):
            cdc.Manifest.parse(text)

    def test_08_recursion_error_from_json_is_converted(self):
        """Surumden bagimsiz: json.loads RecursionError atarsa parse yine
        ValueError verir (3.11-3.13'te 5000 seviye yetiyor, 3.14'te degil)."""
        with mock.patch.object(cdc.json, "loads",
                               side_effect=RecursionError("derin")):
            with self.assertRaises(ValueError):
                cdc.Manifest.parse("{}")


def reference_chunk_ranges(buf, min_size=cdc.MIN_CHUNK,
                           avg_size=cdc.AVG_CHUNK, max_size=cdc.MAX_CHUNK):
    """1.7 oncesi parcalayici (birebir kopya): sinir kararliligi referansi.

    Hizlandirilmis surum bununla ayni sonucu vermek zorunda; bu yuzden
    burada, uretim kodunda degil, testte tutulur."""
    gear = cdc.GEAR
    mask_s, mask_l = cdc._masks(avg_size)
    n = len(buf)
    if min_size < 1:
        min_size = 1
    if max_size < min_size:
        max_size = min_size
    i = 0
    while i < n:
        left = n - i
        if left <= min_size:
            yield i, n
            return
        limit = n if left <= max_size else i + max_size
        mid = min(i + avg_size, limit)
        j = i + min_size
        h = 0
        cut = 0
        while j < mid:
            h = ((h << 1) + gear[buf[j]]) & cdc.M64
            j += 1
            if not h & mask_s:
                cut = j
                break
        if not cut:
            while j < limit:
                h = ((h << 1) + gear[buf[j]]) & cdc.M64
                j += 1
                if not h & mask_l:
                    cut = j
                    break
            if not cut:
                cut = limit
        yield i, cut
        i = cut


class ChunkerStabilityTests(unittest.TestCase):
    """Hizlandirilmis parcalayici 1.6 algoritmasiyla BAYT BAYT ayni
    sinirlari uretmeli: yayinlanmis `.ferdim-merkle` bildirimleri gecerli
    kalsin. Referans yukarida birebir kopya olarak durur."""

    def test_40_matches_reference_on_random_data(self):
        sizes = (0, 1, 7, 8, 9, 4096, 200_000, 700_000, 3 << 20)
        extra = [dict(min_size=1, avg_size=1 << 15, max_size=1 << 20),
                 dict(min_size=1, avg_size=512, max_size=1 << 20),
                 dict(min_size=1 << 17, avg_size=1 << 18, max_size=1 << 19),
                 dict(min_size=300, avg_size=1 << 20, max_size=300)]
        for size in sizes:
            data = hashlib.shake_256(b"cdc-ref-%d" % size).digest(size)
            pars = [{}, SMALL]
            if 200_000 <= size < 3 << 20:      # kucuk girdilerde gereksiz
                pars += extra
            for par in pars:
                with self.subTest(size=size, par=par):
                    self.assertEqual(
                        list(reference_chunk_ranges(data, **par)),
                        list(cdc.chunk_ranges(data, **par)))

    def test_41_matches_reference_on_edge_patterns(self):
        """Tekrar eden/dusuk entropili veri: on eleme adaylari siklasir."""
        pats = {
            "sifir": b"\x00" * 300_000,
            "tek": b"Z" * 300_000,
            "periyodik": b"0123456789abcdef" * 18_750,
            "sinirli": bytes(range(4)) * 75_000,
        }
        # aday yogunlugu yuksek veri: her bayt, dusuk 8 biti sifirlayacak
        # sekilde secilir (koti niyetli girdi taklidi)
        res = {}
        for b in range(256):
            res.setdefault(cdc.GEAR[b] % 256, b)
        h, out = 0, bytearray()
        for _ in range(300_000):
            b = res.get((-2 * h) % 256, 0)
            out.append(b)
            h = ((h << 1) + cdc.GEAR[b]) & cdc.M64
        pats["yogun-aday"] = bytes(out)
        for name, data in pats.items():
            for par in ({}, SMALL):
                with self.subTest(desen=name, par=par):
                    self.assertEqual(
                        list(reference_chunk_ranges(data, **par)),
                        list(cdc.chunk_ranges(data, **par)))

    def test_43_dense_candidates_fall_back_to_the_plain_loop(self):
        """Aday siniri asilinca toptan tarama klasik donguye doner (hazir
        pencere ozetiyle) ve sinirlar degismez."""
        par = dict(min_size=4096, avg_size=1 << 17, max_size=1 << 18)
        data = hashlib.shake_256(b"cdc-yogun").digest(1 << 20)
        want = list(reference_chunk_ranges(data, **par))
        self.assertGreater(len(want), 2)
        with mock.patch.object(cdc, "_MAX_CAND", 1):
            self.assertEqual(list(cdc.chunk_ranges(data, **par)), want)

    def _synthetic_gear(self):
        """Yapay gear tablosu: yalniz bayt 1 hash'e katkir. Boylece h'nin
        dusuk `nbits` biti 'son nbits bayt sifir mi' sorusuna indirgenir ve
        maske eslesmesi ISTENEN konuma konabilir."""
        gear = tuple(1 if i == 1 else 0 for i in range(256))
        map_lo = bytes.maketrans(bytes(range(256)),
                                 bytes(g & 0xFF for g in gear))
        return gear, map_lo

    def test_44_bulk_scan_never_cuts_past_the_limit(self):
        """Toptan tarama hi+1 konumunu aday sayiyordu: parca max_size+1
        bayt olur ve Manifest.parse bildirimi reddeder (klasik dongu ise
        limiti dondurur). Maske eslesmesi TAM olarak limit+1'e konur."""
        gear, map_lo = self._synthetic_gear()
        avg, max_size = 1 << 20, 100          # mask_s 22 bit: 22 bayt pencere
        nbits = cdc._masks(avg)[0].bit_length()
        self.assertEqual(nbits, 22)
        # govde '1', kuyruk sifir: ilk maske eslesmesi son '1'den nbits+1
        # sonra, yani tam olarak max_size+1 konumunda
        body = max_size + 1 - nbits
        data = b"\x01" * body + b"\x00" * (nbits + 41)
        par = dict(min_size=1, avg_size=avg, max_size=max_size)
        with mock.patch.object(cdc, "GEAR", gear), \
                mock.patch.object(cdc, "_MAP_LO", map_lo), \
                mock.patch.object(cdc, "_BULK_MIN", 1):
            want = list(reference_chunk_ranges(data, **par))
            got = list(cdc.chunk_ranges(data, **par))
            self.assertEqual(want[0], (0, max_size))
            self.assertEqual(got, want)
            # dogrudan cagri da hi+1 hicbir zaman donmemeli
            mask_s = cdc._masks(avg)[0]
            self.assertEqual(cdc._first_cut(data, 2, max_size, mask_s, 1), 0)

    def test_45_equivalence_near_max_boundaries(self):
        """max_size sinirina yakin boylarda toptan tarama klasik donguyle
        ayni sinirlari vermeli (hicbir parca max_size'i asmaz)."""
        pars = [dict(min_size=1, avg_size=1 << 15, max_size=1 << 17),
                dict(min_size=4096, avg_size=1 << 16, max_size=1 << 18),
                dict(min_size=1024, avg_size=1 << 17, max_size=1 << 17)]
        for k, par in enumerate(pars):
            mx = par["max_size"]
            for size in sorted({mx - 1, mx, mx + 1, 2 * mx + 3}):
                data = hashlib.shake_256(
                    b"cdc-max-%d-%d" % (k, size)).digest(size)
                with self.subTest(par=par, size=size):
                    want = list(reference_chunk_ranges(data, **par))
                    got = list(cdc.chunk_ranges(data, **par))
                    self.assertEqual(got, want)
                    for s, e in got:
                        self.assertLessEqual(e - s, mx)
                        self.assertLessEqual(e, size)

    def test_42_buffer_types_agree(self):
        data = hashlib.shake_128(b"cdc-buf").digest(900_000)
        want = list(cdc.chunk_ranges(data, **SMALL))
        self.assertEqual(list(cdc.chunk_ranges(memoryview(data), **SMALL)),
                         want)
        root = tmp_root(self, "ferdim-cdc-mmap-")
        p = root / "x.bin"
        p.write_bytes(data)
        with open(p, "rb") as f, mmap.mmap(f.fileno(), 0,
                                           access=mmap.ACCESS_READ) as mm:
            self.assertEqual(list(cdc.chunk_ranges(mm, **SMALL)), want)
        self.assertEqual([h for _, _, h in cdc.chunk_hashes(p, **SMALL)],
                         [sha(data[s:e]) for s, e in want])


class CdcServer(BaseHTTPRequestHandler):
    """Range destekli test sunucusu: istenen araliklari kaydeder ve
    istenirse tek bir bayti bozar (ilk N yanitta)."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        path = urllib.parse.urlsplit(self.path).path
        if path == "/cdc.bin":
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, b = rng[6:].split("-", 1)
                a = int(a)
                b = min(int(b) if b else len(PAYLOAD) - 1, len(PAYLOAD) - 1)
                body = bytearray(PAYLOAD[a:b + 1])
                if b > a:                      # yoklama (0-0) sayilmaz
                    with srv.lock:
                        srv.ranges.append((a, b))
                        if srv.corrupt_at is not None \
                                and a <= srv.corrupt_at <= b \
                                and srv.corrupt_left:
                            body[srv.corrupt_at - a] ^= 0xFF
                            srv.corrupt_left -= 1
                self.send_response(206)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Range",
                                 f"bytes {a}-{b}/{len(PAYLOAD)}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", '"cdc"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", '"cdc"')
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
            return
        if path == "/cdc.bin.ferdim-merkle":
            body = srv.manifest.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def start_cdc_server(manifest: str, corrupt_at=None, corrupt_left=1):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), CdcServer)
    srv.daemon_threads = True
    srv.lock = threading.Lock()
    srv.ranges = []
    srv.manifest = manifest
    srv.corrupt_at = corrupt_at
    srv.corrupt_left = corrupt_left
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class EngineMerkleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tmp_root(cls, "ferdim-cdc-env-")
        cls.env = AppEnv(root=cls.root)
        cls.cfg = cls.env.load_config()
        cls.cfg["max_concurrent"] = 4
        src = cls.root / "remote.bin"
        src.write_bytes(PAYLOAD)
        cls.man = cdc.make_manifest(src)
        cls.man_text = cls.man.dumps()
        cls.man_path = cdc.write_manifest(cls.man,
                                          cls.root / "remote.bin.ferdim-merkle")
        cls.bad_at = len(PAYLOAD) // 3
        cls.bad_off, cls.bad_len = cls.man.offset_of(cls.bad_at)
        assert cls.bad_off >= 0

    def _job(self, url, **kw):
        return cdc_job(self.env, self.cfg, url, **kw)

    def test_10_repairs_only_the_bad_chunk(self):
        srv, base = start_cdc_server(self.man_text, self.bad_at, 1)
        try:
            job = self._job(f"{base}/cdc.bin", segments=1,
                            merkle=str(self.man_path))
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"{job.state} {job.error}")
            self.assertEqual(job.state, JobState.DONE, job.error)
            self.assertEqual(sha(job.final_path.read_bytes()), sha(PAYLOAD))
            self.assertEqual(job.snapshot()["merkle"], "repaired")
            self.assertIn("merkle", job.snapshot()["verified"])
            # onarim yalnizca bozuk parcayi istemeli
            first, repair = phases(srv.ranges, len(PAYLOAD))
            self.assertEqual(sum(b - a + 1 for a, b in first), len(PAYLOAD))
            self.assertTrue(repair, srv.ranges)
            for a, b in repair:
                self.assertGreaterEqual(a, self.bad_off, repair)
                self.assertLessEqual(b, self.bad_off + self.bad_len - 1, repair)
            self.assertEqual(sum(b - a + 1 for a, b in repair), self.bad_len)
            self.assertEqual(job.extra["merkle_repaired_bytes"], self.bad_len)
        finally:
            srv.shutdown()

    def test_11_repair_cap_ends_with_error(self):
        srv, base = start_cdc_server(self.man_text, self.bad_at, 999)
        try:
            job = self._job(f"{base}/cdc.bin", segments=1,
                            merkle=str(self.man_path))
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"{job.state} {job.error}")
            self.assertEqual(job.state, JobState.ERROR)
            self.assertEqual(job.error_code, "merkle_mismatch")
            self.assertEqual(job.snapshot()["merkle"], "mismatch")
            covering = [r for r in srv.ranges
                        if r[0] <= self.bad_at <= r[1]]
            self.assertEqual(len(covering), 1 + MERKLE_REPAIR_MAX, srv.ranges)
        finally:
            srv.shutdown()

    def test_12_sidecar_manifest_is_discovered(self):
        srv, base = start_cdc_server(self.man_text)
        try:
            job = self._job(f"{base}/cdc.bin", segments=2)
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"{job.state} {job.error}")
            self.assertEqual(job.state, JobState.DONE, job.error)
            self.assertEqual(job.snapshot()["merkle"], "ok")
            self.assertEqual(sha(job.final_path.read_bytes()), sha(PAYLOAD))
        finally:
            srv.shutdown()

    def test_13_manifest_url_repairs(self):
        srv, base = start_cdc_server(self.man_text, self.bad_at, 1)
        try:
            job = self._job(f"{base}/cdc.bin", segments=2,
                            merkle=f"{base}/cdc.bin.ferdim-merkle")
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"{job.state} {job.error}")
            self.assertEqual(job.state, JobState.DONE, job.error)
            self.assertEqual(sha(job.final_path.read_bytes()), sha(PAYLOAD))
            self.assertEqual(job.snapshot()["merkle"], "repaired")
        finally:
            srv.shutdown()

    def test_14_bad_manifest_fails_the_job(self):
        bad = self.root / "bozuk.ferdim-merkle"
        bad.write_text("{}", "utf-8")
        srv, base = start_cdc_server(self.man_text)
        try:
            job = self._job(f"{base}/cdc.bin", segments=1, merkle=str(bad))
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)))
            self.assertEqual(job.state, JobState.ERROR)
            self.assertEqual(job.error_code, "merkle_manifest")
        finally:
            srv.shutdown()

    def test_15_no_manifest_no_merkle_state(self):
        srv, base = start_cdc_server("", corrupt_at=None)
        try:
            job = self._job(f"{base}/cdc.bin", segments=2)
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"{job.state} {job.error}")
            self.assertEqual(job.state, JobState.DONE, job.error)
            self.assertEqual(job.snapshot()["merkle"], "")
        finally:
            srv.shutdown()


class UpdateTests(unittest.TestCase):
    def test_20_only_changed_chunks_are_fetched(self):
        root = tmp_root(self, "ferdim-cdc-upd-")
        src = root / "remote.bin"
        src.write_bytes(PAYLOAD)
        man = cdc.make_manifest(src)
        # yerel kopya: basina ekleme (kayma) + bir bolgede bozulma
        broken_from = 3_000_000
        local = bytearray(PAYLOAD[:100_000] + b"EKLENEN" + PAYLOAD[100_000:])
        local[broken_from:broken_from + 4096] = b"\x00" * 4096
        localp = root / "local.bin"
        localp.write_bytes(bytes(local))
        # bagimsiz olcu: baytlari yerelde bulunmayan parcalar indirilmeli
        want = sum(ln for off, ln, _ in man.chunks
                   if bytes(local).find(PAYLOAD[off:off + ln]) < 0)
        srv, base = start_cdc_server(man.dumps())
        try:
            out = root / "out.bin"
            res = cdc.update_file(localp, f"{base}/cdc.bin", man, out,
                                  httpcli.DlOptions(timeout=20))
            self.assertEqual(sha(out.read_bytes()), sha(PAYLOAD))
            self.assertEqual(res["fetched"], want)
            self.assertEqual(res["reused"], man.size - want)
            self.assertLess(res["fetched"], len(PAYLOAD) // 2)
            self.assertEqual(sum(b - a + 1 for a, b in srv.ranges), want)
        finally:
            srv.shutdown()

    def test_21_same_file_needs_no_fetch(self):
        root = tmp_root(self, "ferdim-cdc-upd2-")
        src = root / "remote.bin"
        src.write_bytes(PAYLOAD)
        man = cdc.make_manifest(src)
        srv, base = start_cdc_server(man.dumps())
        try:
            out = root / "out.bin"
            res = cdc.update_file(src, f"{base}/cdc.bin", man, out,
                                  httpcli.DlOptions(timeout=20))
            self.assertEqual(res["fetched"], 0)
            self.assertEqual(res["reused"], len(PAYLOAD))
            self.assertEqual(srv.ranges, [])
            self.assertEqual(sha(out.read_bytes()), sha(PAYLOAD))
        finally:
            srv.shutdown()


class GuardTests(unittest.TestCase):
    """Inceleme bulgulari: dur/iptal, kaynak (provenance), sinirlar."""

    @classmethod
    def setUpClass(cls):
        cls.root = tmp_root(cls, "ferdim-cdc-guard-")
        cls.env = AppEnv(root=cls.root)
        cls.cfg = cls.env.load_config()
        src = cls.root / "remote.bin"
        src.write_bytes(PAYLOAD)
        cls.man = cdc.make_manifest(src)
        cls.man_text = cls.man.dumps()
        cls.man_path = cdc.write_manifest(
            cls.man, cls.root / "remote.bin.ferdim-merkle")
        cls.bad_at = len(PAYLOAD) // 3
        assert cls.man.offset_of(cls.bad_at)[0] >= 0

    def _job(self, url, **kw):
        return cdc_job(self.env, self.cfg, url, **kw)

    # ---------------------------------------------------------- dogrulama dur
    def test_07_verify_halt_stops_early(self):
        root = tmp_root(self, "ferdim-cdc-halt-")
        p = root / "f.bin"
        p.write_bytes(PAYLOAD)
        man = cdc.make_manifest(p, **SMALL)
        self.assertGreater(len(man.chunks), 20)
        # dur sinyali once sorulur: dosya hic acilmaz
        self.assertIsNone(cdc.verify_file(root / "yok.bin", man,
                                          halt=lambda: True))
        calls = []

        def halt():
            calls.append(1)
            return len(calls) >= 5

        self.assertIsNone(cdc.verify_file(p, man, halt=halt))
        self.assertLessEqual(len(calls), 6, "durdurma gecikmemeli")
        self.assertEqual(cdc.verify_file(p, man), (True, []))

    def test_16_cancel_during_repair_decision_wins(self):
        """Onarim karari verilirken gelen iptal DONE/onarim yazmaz."""
        srv, base = start_cdc_server(self.man_text, self.bad_at, 1)
        try:
            job = self._job(f"{base}/cdc.bin", segments=1,
                            merkle=str(self.man_path))
            real = cdc.verify_file
            fired = {"n": 0}

            def hooked(path, man, halt=None):
                res = real(path, man, halt=halt)
                if not fired["n"]:
                    fired["n"] = 1
                    job.cancel()     # monitor is parcayi dogrularken
                return res

            with mock.patch.object(job_mod.cdc, "verify_file", hooked):
                job.start()
                self.assertTrue(wait_state(job, (JobState.CANCELED,), 30),
                                job.state)
            self.assertEqual(job.state, JobState.CANCELED)
            self.assertFalse(job.final_path.exists(), "iptal DONE'a donusmemeli")
            self.assertNotIn("merkle_repaired_bytes", job.extra)
            first, repair = phases(srv.ranges, len(PAYLOAD))
            self.assertEqual(repair, [], "iptalden sonra onarim istegi olmamali")
        finally:
            srv.shutdown()

    def test_17_pause_across_repair_verification_is_synchronous(self):
        """Dogrulama uzun surerken gelen duraklatma: pause() dondugunde
        durum 'paused' ve denetim noktasi yazilmis olmali."""
        srv, base = start_cdc_server(self.man_text, self.bad_at, 1)
        try:
            job = self._job(f"{base}/cdc.bin", segments=1,
                            merkle=str(self.man_path))
            real = cdc.verify_file
            fired = {"n": 0}
            seen: dict = {}

            def do_pause():
                job.pause()
                seen["state"] = job.state

            def hooked(path, man, halt=None):
                res = real(path, man, halt=halt)
                if not fired["n"]:
                    fired["n"] = 1
                    threading.Thread(target=do_pause, daemon=True,
                                     name="pause-merkle").start()
                    end = time.monotonic() + 10
                    while not job._stop.is_set() and time.monotonic() < end:
                        time.sleep(0.005)
                return res

            with mock.patch.object(job_mod.cdc, "verify_file", hooked):
                job.start()
                self.assertTrue(wait_state(job, (JobState.PAUSED,), 30),
                                job.state)
                th = next((t for t in threading.enumerate()
                           if t.name == "pause-merkle"), None)
                if th is not None:
                    th.join(20)
            self.assertEqual(seen.get("state"), JobState.PAUSED)
            self.assertTrue(job.meta_path.exists(), "denetim noktasi yok")
            self.assertTrue(job.part_path.exists(), "yarim dosya korunmali")
            self.assertFalse(job.final_path.exists())
            job.cancel(delete_files=True)
        finally:
            srv.shutdown()

    # ------------------------------------------------------- bildirim kaynagi
    def test_18_auto_manifest_mismatch_warns_without_repair(self):
        """Kendiliginden bulunan yan dosya bayat olabilir: onarim denenmez,
        is DONE kalir ve uyari kodu yazilir."""
        srv, base = start_cdc_server(self.man_text, self.bad_at, 1)
        try:
            job = self._job(f"{base}/cdc.bin", segments=1)
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"{job.state} {job.error}")
            self.assertEqual(job.state, JobState.DONE, job.error)
            snap = job.snapshot()
            self.assertEqual(snap["merkle"], "warn")
            self.assertIn("merkle_mismatch", snap["warning_codes"])
            first, repair = phases(srv.ranges, len(PAYLOAD))
            self.assertEqual(repair, [], "yan dosya bildirimi onarilmamali")
            self.assertNotIn("merkle_repaired_bytes", job.extra)
        finally:
            srv.shutdown()

    def test_19_auto_manifest_size_mismatch_warns_only(self):
        other = cdc.Manifest.of(chunk_bytes(PAYLOAD[: 2 << 20]))
        srv, base = start_cdc_server(other.dumps())
        try:
            job = self._job(f"{base}/cdc.bin", segments=1)
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"{job.state} {job.error}")
            self.assertEqual(job.state, JobState.DONE, job.error)
            self.assertEqual(job.snapshot()["merkle"], "warn")
            self.assertEqual(job.extra["merkle_note"], "size")
            self.assertIn("merkle_mismatch",
                          job.snapshot()["warning_codes"])
        finally:
            srv.shutdown()

    def test_24_explicit_manifest_size_mismatch_errors(self):
        short = cdc.Manifest.of(chunk_bytes(PAYLOAD[: 2 << 20]))
        path = cdc.write_manifest(short, tmp_root(self, "ferdim-cdc-short-")
                                  / "short.ferdim-merkle")
        srv, base = start_cdc_server(short.dumps())
        try:
            job = self._job(f"{base}/cdc.bin", segments=1, merkle=str(path))
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"{job.state} {job.error}")
            self.assertEqual(job.state, JobState.ERROR)
            self.assertEqual(job.error_code, "merkle_mismatch")
            self.assertIn("boyut", job.error)
        finally:
            srv.shutdown()

    # --------------------------------------------------------------- sinirlar
        size = cdc.MIN_CHUNK * 4

        def doc(chunks):
            return json.dumps({"v": 1, "algo": cdc.ALGO, "size": size,
                               "root": "0" * 64, "chunks": chunks})

        with self.assertRaisesRegex(ValueError, "ust sinir"):
            cdc.Manifest.parse(doc([[0, cdc.MAX_CHUNK + 1, "a" * 64]]))
        with self.assertRaisesRegex(ValueError, "alt sinir"):
            cdc.Manifest.parse(doc([[0, 1024, "a" * 64],
                                    [1024, size - 1024, "b" * 64]]))
        ok = cdc.Manifest.of([(0, cdc.MIN_CHUNK, sha(b"x")),
                              (cdc.MIN_CHUNK, 1024, sha(b"y"))])
        self.assertEqual(cdc.Manifest.parse(ok.dumps()), ok)

    def test_20_local_manifest_is_bounded_and_regular(self):
        root = tmp_root(self, "ferdim-cdc-lim-")
        big = root / "big.ferdim-merkle"
        big.touch()
        os.truncate(big, cdc.MAX_MANIFEST + 1)
        env = AppEnv(root=root)
        cfg = env.load_config()
        srv, base = start_cdc_server(self.man_text)
        try:
            for path, why in ((str(big), "cok buyuk"),
                              ("/dev/null", "normal dosya degil"),
                              (str(root / "yok.ferdim-merkle"), "okunamadi")):
                with self.subTest(path=path):
                    job = cdc_job(env, cfg, f"{base}/cdc.bin", segments=1,
                                  merkle=path)
                    job.start()
                    self.assertTrue(wait_state(job, (JobState.ERROR,
                                                    JobState.DONE), 30),
                                    job.state)
                    self.assertEqual(job.state, JobState.ERROR, job.state)
                    self.assertEqual(job.error_code, "merkle_manifest")
                    self.assertIn(why, job.error)
        finally:
            srv.shutdown()

    def test_21_cli_absolutises_relative_merkle(self):
        from ferdim.cli import _abs_merkle
        self.assertEqual(_abs_merkle("m.ferdim-merkle"),
                         os.path.abspath("m.ferdim-merkle"))
        self.assertEqual(_abs_merkle("~/m.ferdim-merkle"),
                         os.path.abspath(os.path.expanduser("~/m.ferdim-merkle")))
        url = "https://ornek.example/x.ferdim-merkle"
        self.assertEqual(_abs_merkle(url), url)
        self.assertEqual(_abs_merkle(""), "")
        cli = (Path(__file__).resolve().parents[1] / "ferdim" / "cli.py")
        self.assertIn("merkle=_abs_merkle(a.merkle)",
                      cli.read_text("utf-8"))

    def test_22_merkle_is_http_only(self):
        env = AppEnv(root=tmp_root(self, "ferdim-cdc-kind-"))
        cfg = env.load_config()
        for url in ("ftp://127.0.0.1:1/x.bin",
                    "http://127.0.0.1:1/media.m3u8",
                    "http://127.0.0.1:1/manifest.mpd"):
            with self.subTest(url=url):
                job = cdc_job(env, cfg, url, segments=1,
                              merkle=str(self.man_path))
                job.start()
                self.assertTrue(wait_state(job, (JobState.ERROR,
                                                JobState.DONE), 30),
                                job.state)
                self.assertEqual(job.state, JobState.ERROR, job.state)
                self.assertEqual(job.error_code, "merkle_manifest")
                self.assertIn("HTTP(S)", job.error)

    # ------------------------------------------------------------ hiz ornekleri
    def test_23_repair_rebases_speed_marks(self):
        root = tmp_root(self, "ferdim-cdc-marks-")
        env = AppEnv(root=root)
        cfg = env.load_config()
        good = root / "remote.bin"
        good.write_bytes(PAYLOAD)
        man = cdc.make_manifest(good)
        srv, base = start_cdc_server(self.man_text)
        self.addCleanup(srv.shutdown)
        job = cdc_job(env, cfg, f"{base}/cdc.bin", segments=1)
        job.kind = "http"
        job.total = job.done = man.size
        raw = bytearray(PAYLOAD)
        off, ln = man.chunks[1][0], man.chunks[1][1]
        raw[off + 5] ^= 0xFF
        job.target_dir.mkdir(parents=True, exist_ok=True)
        job.part_path.write_bytes(bytes(raw))
        job._fd = os.open(str(job.part_path), os.O_RDWR)
        job._merkle = man
        job.state = JobState.DOWNLOADING
        job._speed_done_mark = man.size      # eski olcum: tam boy
        job._gov["done"] = man.size
        try:
            self.assertTrue(job._merkle_repair())
            self.assertEqual(job.done, man.size - ln)
            self.assertEqual(job._speed_done_mark, job.done,
                             "hiz ornegi negatife dusmemeli")
            self.assertEqual(job._gov["done"], job.done)
        finally:
            job.cancel(delete_files=True)


class ApiPersistenceTests(unittest.TestCase):
    """`--merkle` API'den gecer ve kuyruk yeniden yuklenince kaybolmaz."""

    def test_30_merkle_survives_api_and_restart(self):
        from ferdim import api
        from ferdim.engine.queue import QueueManager
        env = AppEnv(root=tmp_root(self, "ferdim-cdc-q-"))
        cfg = env.load_config()
        cfg["max_concurrent"] = 1
        cfg["schedule"] = {"enabled": True, "start": "00:00", "stop": "00:00"}
        qm = QueueManager(env, cfg)
        try:
            ctx = {"qm": qm, "env": env, "cfg": cfg,
                   "set_cfg": env.save_config}
            r = api.handle(ctx, "add", {
                "url": "http://127.0.0.1:1/x.bin", "paused": True,
                "merkle": "/tmp/x.bin.ferdim-merkle"})
            self.assertTrue(r.get("ok"), r)
            job = qm.get(r["id"])
            self.assertEqual(job.spec.merkle, "/tmp/x.bin.ferdim-merkle")
            qm._save()
        finally:
            qm.shutdown()
        qm2 = QueueManager(env, cfg)
        try:
            self.assertEqual(qm2.get(r["id"]).spec.merkle,
                             "/tmp/x.bin.ferdim-merkle")
        finally:
            qm2.shutdown()


def cdc_job(env, cfg, url, **kw):
    from ferdim.engine.job import DownloadJob
    return DownloadJob(env, cfg, JobSpec(url=url, **kw))


if __name__ == "__main__":
    unittest.main(verbosity=2)
