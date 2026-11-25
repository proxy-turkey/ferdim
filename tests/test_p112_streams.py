"""ROADMAP 1.12 Lane B: HLS/DASH akis dayanikliligi.

Kapsam:
- segment basina gecici hata yeniden denemesi (429/503/5xx/timeout),
  kalici hatalarin (404/403/auth) hemen bitmesi
- HLS EXT-X-BYTERANGE alt araliklari (offset varsayilani, 206 dogrulama)
- HLS EXT-X-KEY AES-128 (IV/medya sirasi, rotasyon, METHOD=NONE, DRM reddi)
- DASH coklu Period birlestirme + kardes ses dosyasi
- manifest boyut siniri (manifest_too_large)

Testler yerel HTTP sunucusuna karsi calisir; bekleme suresi enjekte
edilebilir (`segments.retry_delay`) oldugu icin uyku tabanli zamanlama yok.
"""
from __future__ import annotations

import builtins
import json
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

from ferdim.engine import dash, hls, httpcli, segments
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.paths import AppEnv
from ferdim.ratelimit import TokenBucket
from tests.helpers import tmp_root

try:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    AES_OK = True
except Exception:                     # opsiyonel bagimlilik yok
    AES_OK = False


# ------------------------------------------------------------------ server
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/octet-stream",
              extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urllib.parse.urlsplit(self.path).path
        st = self.server.state
        with st["lock"]:
            st["counts"][p] = st["counts"].get(p, 0) + 1
            st["ranges"].append((p, self.headers.get("Range", "")))
            n = st["flaky"].get(p, 0)
            if n:
                st["flaky"][p] = n - 1
        ctype = st["types"].get(p, "application/octet-stream")
        if n:
            self._send(503, b"", ctype, [("Retry-After", "0")])
            return
        if p in st["range_files"]:
            data = st["range_files"][p]
            rng = self.headers.get("Range") or ""
            if rng.startswith("bytes="):
                a, b = rng[6:].split("-", 1)
                a = int(a)
                b = min(int(b) if b else len(data) - 1, len(data) - 1)
                body = data[a:b + 1]
                self._send(206, body, ctype, [
                    ("Content-Range", f"bytes {a}-{b}/{len(data)}"),
                    ("Accept-Ranges", "bytes")])
            else:
                self._send(200, data, ctype, [("Accept-Ranges", "bytes")])
            return
        if p in st["no_range"]:
            self._send(200, st["no_range"][p], ctype)
            return
        if p in st["files"]:
            self._send(200, st["files"][p], ctype)
            return
        self._send(404, b"yok")


def start_stream_server() -> tuple[ThreadingHTTPServer, str, dict]:
    st = {"files": {}, "range_files": {}, "no_range": {}, "types": {},
          "flaky": {}, "counts": {}, "ranges": [], "lock": threading.Lock()}
    ThreadingHTTPServer.request_queue_size = 128
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.state = st
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", st


def wait_state(job, states, timeout=30):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if job.state in states:
            return True
        time.sleep(0.05)
    return False


def aes_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = tmp_root(self, "ferdim-p112-")
        self.srv, self.base, self.st = start_stream_server()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.opt = httpcli.DlOptions(user_agent="test", timeout=10, retries=3)

    def assemble(self, entries, stop=None):
        out = self.root / "out.bin"
        meta_path = self.root / "meta.json"
        meta: dict = {}
        fd = os.open(out, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            segments.assemble(self.opt, fd, meta, stop or threading.Event(),
                              TokenBucket(0), None, meta_path, None, entries)
        finally:
            os.close(fd)
        return out.read_bytes(), meta

    def download_hls(self, playlist: str, path: str = "/media.m3u8"):
        self.st["files"][path] = playlist.encode()
        self.st["types"][path] = "application/vnd.apple.mpegurl"
        out = self.root / "hls-out.ts"
        meta_path = self.root / "hls-meta.json"
        meta: dict = {}
        fd = os.open(out, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            hls.download_hls(self.base + path, self.opt, fd, meta,
                             threading.Event(), TokenBucket(0), None, {},
                             meta_path=meta_path)
        finally:
            os.close(fd)
        return out.read_bytes(), meta, meta_path


# ------------------------------------------------------- 1) segment retries
class SegmentRetryTests(_Base):
    def test_transient_error_is_retried(self):
        data = b"segment-govdesi"
        self.st["files"]["/flaky/seg"] = data
        self.st["flaky"]["/flaky/seg"] = 2
        with mock.patch.object(segments, "retry_delay", return_value=0):
            out, _ = self.assemble([segments.Segment(self.base + "/flaky/seg")])
        self.assertEqual(out, data)
        self.assertEqual(self.st["counts"]["/flaky/seg"], 3,
                         "iki gecici hata iki kez yeniden denenmeliydi")

    def test_terminal_404_is_not_retried(self):
        with mock.patch.object(segments, "retry_delay", return_value=0):
            with self.assertRaises(RuntimeError):
                self.assemble([segments.Segment(self.base + "/yok")])
        self.assertEqual(self.st["counts"]["/yok"], 1,
                         "404 kalici: yeniden denenmemeli")

    def test_retries_are_bounded_then_fail(self):
        self.st["files"]["/flaky/hep"] = b"x"
        self.st["flaky"]["/flaky/hep"] = 99
        with mock.patch.object(segments, "retry_delay", return_value=0):
            with self.assertRaises(RuntimeError):
                self.assemble([segments.Segment(self.base + "/flaky/hep")])
        self.assertEqual(self.st["counts"]["/flaky/hep"], 1 + self.opt.retries)

    def test_one_transient_error_does_not_abort_2000_segment_stream(self):
        """Tek gecici hata 2000 segmentlik akisi durdurmamali."""
        target = "http://ornek/1234.ts"
        seen = set()

        def fake_fetch(url, opt, bucket, stop=None):
            if url == target and url not in seen:
                seen.add(url)
                raise TimeoutError("gecici zaman asimi")
            return b"S"

        entries = [segments.Segment(f"http://ornek/{i}.ts") for i in range(2000)]
        with mock.patch.object(segments, "fetch_all", side_effect=fake_fetch), \
                mock.patch.object(segments, "retry_delay", return_value=0):
            out, meta = self.assemble(entries)
        self.assertEqual(len(out), 2000)
        self.assertEqual(meta["segments_total"], 2000)


# ---------------------------------------------------------- 2) byteranges
class HlsByteRangeTests(_Base):
    def test_parse_byterange_defaults_to_previous_end(self):
        playlist = "\n".join([
            "#EXTM3U", "#EXT-X-VERSION:4",
            "#EXT-X-BYTERANGE:10@0", "#EXTINF:1,", "sub.bin",
            "#EXT-X-BYTERANGE:10", "#EXTINF:1,", "sub.bin",
            "#EXT-X-BYTERANGE:5@100", "#EXTINF:1,", "sub.bin",
            "#EXT-X-ENDLIST", ""])
        pl = hls.parse_media(playlist, self.base + "/")
        self.assertEqual([(s.start, s.length) for s in pl.segments],
                         [(0, 10), (10, 10), (100, 5)])
        self.assertEqual(len({s.url for s in pl.segments}), 1)

    def test_range_fetch_returns_only_the_declared_subrange(self):
        data = bytes(range(256)) * 4
        self.st["range_files"]["/sub.bin"] = data
        entries = [segments.Segment(self.base + "/sub.bin", 0, 10),
                   segments.Segment(self.base + "/sub.bin", 10, 10)]
        out, _ = self.assemble(entries)
        self.assertEqual(out, data[:20])
        got = sorted(r for p, r in self.st["ranges"] if p == "/sub.bin")
        self.assertEqual(got, ["bytes=0-9", "bytes=10-19"])

    def test_server_ignoring_range_is_a_terminal_error(self):
        data = os.urandom(4096)
        self.st["no_range"]["/tam.bin"] = data
        with mock.patch.object(segments, "retry_delay", return_value=0):
            with self.assertRaises(segments.StreamError) as ctx:
                self.assemble([segments.Segment(self.base + "/tam.bin", 0, 10)])
        self.assertIn("Range", str(ctx.exception))
        self.assertEqual(self.st["counts"]["/tam.bin"], 1,
                         "Range yok sayan sunucu tekrar denenmemeli")


# ---------------------------------------------------------- 3) encryption
@unittest.skipUnless(AES_OK, "cryptography kurulu degil")
class HlsAesTests(_Base):
    def _key_playlist(self, key0, key1, iv1):
        return "\n".join([
            "#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-MEDIA-SEQUENCE:10",
            '#EXT-X-KEY:METHOD=AES-128,URI="key0.bin"',
            "#EXTINF:1,", "e0.ts",
            "#EXTINF:1,", "e1.ts",
            f'#EXT-X-KEY:METHOD=AES-128,URI="key1.bin",IV=0x{iv1.hex()}',
            "#EXTINF:1,", "e2.ts",
            "#EXT-X-ENDLIST", ""])

    def test_aes128_rotation_default_iv_and_one_fetch_per_key(self):
        plain = [os.urandom(100 + i) for i in range(3)]
        key0, key1 = os.urandom(16), os.urandom(16)
        iv1 = os.urandom(16)
        self.st["files"]["/key0.bin"] = key0
        self.st["files"]["/key1.bin"] = key1
        self.st["files"]["/e0.ts"] = aes_encrypt(plain[0], key0,
                                                 (10).to_bytes(16, "big"))
        self.st["files"]["/e1.ts"] = aes_encrypt(plain[1], key0,
                                                 (11).to_bytes(16, "big"))
        self.st["files"]["/e2.ts"] = aes_encrypt(plain[2], key1, iv1)
        out, meta, meta_path = self.download_hls(
            self._key_playlist(key0, key1, iv1))
        self.assertEqual(out, b"".join(plain))
        self.assertEqual(self.st["counts"]["/key0.bin"], 1,
                         "ayni anahtar URI'si bir kez cekilmeli")
        self.assertEqual(self.st["counts"]["/key1.bin"], 1)
        # anahtar yalnizca bellekte: meta ve gecici dosyalarda olmamali
        self.assertNotIn("key", json.dumps(meta))
        for f in self.root.rglob("*"):
            if f.is_file():
                blob = f.read_bytes()
                self.assertNotIn(key0, blob, f"anahtar diske yazildi: {f}")
                self.assertNotIn(key1, blob, f"anahtar diske yazildi: {f}")
        self.assertFalse(meta_path.with_name(meta_path.name + ".segs").exists())

    def test_method_none_resets_encryption(self):
        plain0, plain1 = os.urandom(90), os.urandom(50)
        key = os.urandom(16)
        self.st["files"]["/key0.bin"] = key
        self.st["files"]["/e0.ts"] = aes_encrypt(plain0, key,
                                                 (0).to_bytes(16, "big"))
        self.st["files"]["/duz.ts"] = plain1
        playlist = "\n".join([
            "#EXTM3U", '#EXT-X-KEY:METHOD=AES-128,URI="key0.bin"',
            "#EXTINF:1,", "e0.ts",
            "#EXT-X-KEY:METHOD=NONE",
            "#EXTINF:1,", "duz.ts",
            "#EXT-X-ENDLIST", ""])
        pl = hls.parse_media(playlist, self.base + "/")
        self.assertIsNone(pl.segments[1].key_uri,
                          "METHOD=NONE sifrelemeyi sifirlamali")
        out, _, _ = self.download_hls(playlist)
        self.assertEqual(out, plain0 + plain1)

    def test_key_must_be_16_bytes(self):
        self.st["files"]["/kisa.bin"] = b"x" * 15
        self.st["files"]["/e0.ts"] = os.urandom(16)
        playlist = "\n".join([
            "#EXTM3U", '#EXT-X-KEY:METHOD=AES-128,URI="kisa.bin"',
            "#EXTINF:1,", "e0.ts", "#EXT-X-ENDLIST", ""])
        with self.assertRaises(hls.HlsError) as ctx:
            self.download_hls(playlist)
        self.assertEqual(ctx.exception.code, "hls_key_invalid")

    def test_corrupt_ciphertext_reports_hls_decrypt(self):
        key = os.urandom(16)
        self.st["files"]["/key0.bin"] = key
        self.st["files"]["/e0.ts"] = b"kisa"        # blok hizasiz
        playlist = "\n".join([
            "#EXTM3U", '#EXT-X-KEY:METHOD=AES-128,URI="key0.bin"',
            "#EXTINF:1,", "e0.ts", "#EXT-X-ENDLIST", ""])
        with self.assertRaises(segments.StreamError) as ctx:
            self.download_hls(playlist)
        self.assertEqual(ctx.exception.code, "hls_decrypt")


class HlsDrmTests(_Base):
    BASE_PLAYLIST = ("#EXTM3U\n{key}\n#EXTINF:1,\nseg.ts\n#EXT-X-ENDLIST\n")

    def test_sample_aes_methods_are_rejected(self):
        for method in ("SAMPLE-AES", "SAMPLE-AES-CTR"):
            playlist = self.BASE_PLAYLIST.format(
                key=f'#EXT-X-KEY:METHOD={method},URI="k.bin"')
            with self.assertRaises(hls.HlsError) as ctx:
                hls.parse_media(playlist, self.base + "/")
            self.assertEqual(ctx.exception.code, "hls_drm", method)

    def test_non_identity_keyformat_is_rejected(self):
        playlist = self.BASE_PLAYLIST.format(
            key='#EXT-X-KEY:METHOD=AES-128,URI="k.bin",'
                'KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"')
        with self.assertRaises(hls.HlsError) as ctx:
            hls.parse_media(playlist, self.base + "/")
        self.assertEqual(ctx.exception.code, "hls_drm")

    @unittest.skipUnless(AES_OK, "cryptography kurulu degil")
    def test_missing_cryptography_fails_with_hls_crypto_missing(self):
        self.st["files"]["/key0.bin"] = os.urandom(16)
        self.st["files"]["/e0.ts"] = os.urandom(32)
        playlist = self.BASE_PLAYLIST.format(
            key='#EXT-X-KEY:METHOD=AES-128,URI="key0.bin"')
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "cryptography" or name.startswith("cryptography."):
                raise ImportError("cryptography yok")
            return real_import(name, *a, **kw)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaises(hls.HlsError) as ctx:
                self.download_hls(playlist)
        self.assertEqual(ctx.exception.code, "hls_crypto_missing")


# -------------------------------------------------------------- 4) DASH
def _mpd_periods(periods: str) -> str:
    return ('<?xml version="1.0"?><MPD type="static">'
            + periods + '</MPD>')


def _video_period(rep_id: str, seconds: int, extra_ad: str = "") -> str:
    return (
        f'<Period duration="PT{seconds}S">'
        f'<AdaptationSet mimeType="video/mp4">'
        f'<Representation id="{rep_id}" width="10" height="10" '
        f'bandwidth="1"><SegmentTemplate duration="1" timescale="1" '
        f'startNumber="1" initialization="{rep_id}-init.mp4" '
        f'media="{rep_id}-$Number$.m4s"/></Representation></AdaptationSet>'
        + extra_ad + '</Period>')


def _audio_ad(rep_id: str, media: str) -> str:
    return (
        f'<AdaptationSet mimeType="audio/mp4">'
        f'<Representation id="{rep_id}" bandwidth="128000">'
        f'<SegmentTemplate duration="1" timescale="1" startNumber="1" '
        f'initialization="{rep_id}-init.mp4" media="{media}"/>'
        f'</Representation></AdaptationSet>')


class DashPlanTests(unittest.TestCase):
    def test_periods_are_concatenated_with_their_own_inits(self):
        mpd = _mpd_periods(_video_period("v0", 1) + _video_period("v1", 1))
        init, urls = dash.build_segment_list(mpd, "https://cdn.test/v.mp4")
        self.assertEqual(init, "https://cdn.test/v0-init.mp4")
        self.assertEqual(urls, ["https://cdn.test/v0-1.m4s",
                                "https://cdn.test/v1-init.mp4",
                                "https://cdn.test/v1-1.m4s"])

    def test_audio_representation_becomes_a_sibling_plan(self):
        mpd = _mpd_periods(
            _video_period("v0", 2, _audio_ad("a0", "a0-$Number$.m4s")))
        plan = dash.build_plan(mpd, "https://cdn.test/v.mp4")
        self.assertEqual(plan["video"]["segments"],
                         ["https://cdn.test/v0-1.m4s",
                          "https://cdn.test/v0-2.m4s"])
        audio = plan["audio"]
        self.assertIsNotNone(audio)
        self.assertEqual(audio["init_url"], "https://cdn.test/a0-init.mp4")
        self.assertEqual(audio["segments"],
                         ["https://cdn.test/a0-1.m4s",
                          "https://cdn.test/a0-2.m4s"])
        self.assertEqual(audio["ext"], ".m4a")

    def test_audio_across_periods_with_video(self):
        mpd = _mpd_periods(
            _video_period("v0", 1, _audio_ad("a0", "a0-$Number$.m4s"))
            + f'<Period duration="PT1S">'
            + _audio_ad("a1", "a1-$Number$.m4s") + '</Period>')
        # ikinci Period'da video yok: yalnizca ilk Period video verir
        plan = dash.build_plan(mpd, "https://cdn.test/v.mp4")
        self.assertEqual(plan["audio"]["segments"],
                         ["https://cdn.test/a0-1.m4s",
                          "https://cdn.test/a1-init.mp4",
                          "https://cdn.test/a1-1.m4s"])


class DashJobTests(_Base):
    def test_multi_period_video_and_audio_sibling(self):
        v0 = b"v0-init" + b"v0-1" + b"v0-2"
        v1 = b"v1-init" + b"v1-1" + b"v1-2"
        audio = b"a-init" + b"a-1" + b"a-2"
        files = {
            "/vod/v0-init.mp4": b"v0-init", "/vod/v0-1.m4s": b"v0-1",
            "/vod/v0-2.m4s": b"v0-2", "/vod/v1-init.mp4": b"v1-init",
            "/vod/v1-1.m4s": b"v1-1", "/vod/v1-2.m4s": b"v1-2",
            "/vod/a-init.mp4": b"a-init", "/vod/a-1.m4s": b"a-1",
            "/vod/a-2.m4s": b"a-2",
        }
        self.st["files"].update(files)
        mpd = ('<?xml version="1.0"?><MPD type="static">'
               '<Period duration="PT2S">'
               '<AdaptationSet mimeType="video/mp4">'
               '<Representation id="v0" width="10" height="10" bandwidth="1">'
               '<SegmentTemplate duration="1" timescale="1" startNumber="1" '
               'initialization="v0-init.mp4" media="v0-$Number$.m4s"/>'
               '</Representation></AdaptationSet>'
               + _audio_ad("a", "a-$Number$.m4s")
               + '</Period><Period duration="PT2S">'
               '<AdaptationSet mimeType="video/mp4">'
               '<Representation id="v1" width="10" height="10" bandwidth="1">'
               '<SegmentTemplate duration="1" timescale="1" startNumber="1" '
               'initialization="v1-init.mp4" media="v1-$Number$.m4s"/>'
               '</Representation></AdaptationSet></Period></MPD>')
        self.st["files"]["/vod/manifest.mpd"] = mpd.encode()
        self.st["types"]["/vod/manifest.mpd"] = "application/dash+xml"

        env = AppEnv(root=self.root / "env")
        dl = self.root / "dl"
        job = DownloadJob(env, env.load_config(),
                          JobSpec(url=self.base + "/vod/manifest.mpd",
                                  directory=str(dl)))
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                        f"{job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        video_path = Path(job.final_path)
        self.assertEqual(video_path.read_bytes(), v0 + v1)
        audio_path = video_path.with_name("manifest.audio.m4a")
        self.assertTrue(audio_path.is_file(), "kardes ses dosyasi yok")
        self.assertEqual(audio_path.read_bytes(), audio)
        self.assertEqual(job.extra.get("audio"), str(audio_path))


# ------------------------------------------------------ 5) manifest limits
class ManifestLimitTests(_Base):
    def test_hls_manifest_over_limit_fails_with_code(self):
        self.st["files"]["/buyuk.m3u8"] = b"#EXTM3U\n" + b"x" * 2048
        with mock.patch.object(segments, "MANIFEST_MAX", 512):
            with self.assertRaises(segments.StreamError) as ctx:
                hls.fetch_text(self.base + "/buyuk.m3u8", self.opt)
        self.assertEqual(ctx.exception.code, "manifest_too_large")

    def test_dash_manifest_over_limit_fails_with_code(self):
        self.st["files"]["/buyuk.mpd"] = b"<MPD>" + b"x" * 2048 + b"</MPD>"
        with mock.patch.object(segments, "MANIFEST_MAX", 512):
            with self.assertRaises(segments.StreamError) as ctx:
                dash.fetch_mpd(self.base + "/buyuk.mpd", self.opt)
        self.assertEqual(ctx.exception.code, "manifest_too_large")

    def test_job_reports_manifest_too_large_for_hls(self):
        self.st["files"]["/buyuk.m3u8"] = b"#EXTM3U\n" + b"x" * 2048
        env = AppEnv(root=self.root / "env")
        job = DownloadJob(env, env.load_config(),
                          JobSpec(url=self.base + "/buyuk.m3u8",
                                  directory=str(self.root / "dl")))
        with mock.patch.object(segments, "MANIFEST_MAX", 512):
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"{job.state} {job.error}")
        self.assertEqual(job.state, JobState.ERROR)
        self.assertEqual(job.error_code, "manifest_too_large")

    def test_job_reports_manifest_too_large_for_dash(self):
        self.st["files"]["/buyuk.mpd"] = b"<MPD>" + b"x" * 2048 + b"</MPD>"
        env = AppEnv(root=self.root / "env")
        job = DownloadJob(env, env.load_config(),
                          JobSpec(url=self.base + "/buyuk.mpd",
                                  directory=str(self.root / "dl")))
        with mock.patch.object(segments, "MANIFEST_MAX", 512):
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"{job.state} {job.error}")
        self.assertEqual(job.state, JobState.ERROR)
        self.assertEqual(job.error_code, "manifest_too_large")


if __name__ == "__main__":
    unittest.main(verbosity=2)
