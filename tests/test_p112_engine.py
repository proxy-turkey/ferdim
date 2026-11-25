"""1.12 LANE A (motor) — kuyruk geneli hiz, kisa 206, tek akisa dusme,
Retry-After kapisi, fd tukenmesi ve yer onayi.

Kapsam:
- kuyruk geneli TEK token kovasi tum islerce paylasilir; `--limit` is
  basina ek kisit olarak AYRICA uygulanir (8 is x limit = 8x limit degil),
- sunucunun istedigimizden KISA 206 sonu onurlandirilir (kalan aralik
  yeniden kuyruga girer; hata degil),
- Range duyuran sunucu aralik istegine 200 donerse tek akisa dusulur,
- uzun Retry-After is geneli bir kapiya donusur (tek isci uykusu degil) ve
  HTTP-tarih `Date` basligina gore hesaplanir,
- fd tukenmesi (EMFILE/ENFILE) isi HATA'ya degil DURAKLATIR (fd_exhausted)
  ve link sayisi RLIMIT_NOFILE butcesine clamp'lenir,
- boyut bilinen temiz baslangicta .part icin best-effort yer onayi;
  desteklenmeyen dosya sisteminde seyrek dosyaya dusulur.

Testler sabit uykuya dayanmaz: son tarihli yoklamalar ve olay kapilari.
"""
from __future__ import annotations

import email.message
import errno
import hashlib
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import util
from ferdim.engine import job as job_mod
from ferdim.engine.job import DownloadJob, JobSpec, JobState, _retry_after
from ferdim.engine.queue import QueueManager
from ferdim.paths import AppEnv
from ferdim.ratelimit import CombinedBucket, TokenBucket
from tests.helpers import tmp_root

SHORT_DATA = os.urandom(512 * 1024)
SHORT_SHA = hashlib.sha256(SHORT_DATA).hexdigest()
SHORT_ETAG = '"p112-short"'
BIG_DATA = os.urandom(3 * 1024 * 1024 + 1234)
BIG_SHA = hashlib.sha256(BIG_DATA).hexdigest()
SERVE_CAP = 64 * 1024


def wait_until(pred, timeout=40.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def wait_state(job, states, timeout=40):
    return wait_until(lambda: job.state in states, timeout, 0.05)


class _Srv(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, handler, payload):
        super().__init__(("127.0.0.1", 0), handler)
        self.payload = payload
        self.requests = 0
        self.ignored = 0        # aralik istegine 200 donen sayac

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


def serve(handler, payload) -> _Srv:
    srv = _Srv(handler, payload)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self, code, body, extra=()):
        self.server.requests += 1
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _range(self):
        rng = self.headers.get("Range") or ""
        if not rng.startswith("bytes="):
            return None
        a, b = rng[6:].split("-", 1)
        a = int(a)
        b = int(b) if b else len(self.server.payload) - 1
        return a, min(b, len(self.server.payload) - 1)

    def do_GET(self):
        rng = self._range()
        if rng is not None:
            a, b = rng
            b = min(b, a + SERVE_CAP - 1)      # KISA 206: kendi parca siniri
            body = self.server.payload[a:b + 1]
            self._body(206, body, [
                ("Content-Range", f"bytes {a}-{b}/{len(self.server.payload)}"),
                ("Accept-Ranges", "bytes"), ("ETag", SHORT_ETAG)])
        else:
            self._body(200, self.server.payload, [
                ("Accept-Ranges", "bytes"), ("ETag", SHORT_ETAG)])


class _IgnoreHandler(BaseHTTPRequestHandler):
    """Range duyurur (probe 206) ama aralik istegine 200 tam govde doner."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, extra=()):
        self.server.requests += 1
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self):
        total = len(self.server.payload)
        rng = self.headers.get("Range") or ""
        if rng.strip() == "bytes=0-0":
            self._send(206, self.server.payload[:1], [
                ("Content-Range", f"bytes 0-0/{total}"),
                ("Accept-Ranges", "bytes"), ("ETag", SHORT_ETAG)])
            return
        # aralik istegi yok sayilir: 200 + tam govde
        self.server.ignored += 1
        self._send(200, self.server.payload, [
            ("Accept-Ranges", "bytes"), ("ETag", SHORT_ETAG)])


class _429Handler(BaseHTTPRequestHandler):
    """Probe normal; diger aralik istekleri uzun Retry-After ile 429."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, extra=()):
        self.server.requests += 1
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self):
        total = len(self.server.payload)
        rng = self.headers.get("Range") or ""
        if rng.strip() == "bytes=0-0":
            self._send(206, self.server.payload[:1], [
                ("Content-Range", f"bytes 0-0/{total}"),
                ("Accept-Ranges", "bytes"), ("ETag", SHORT_ETAG)])
            return
        self._send(429, b"", [("Retry-After", "100")])


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = AppEnv(root=tmp_root(cls, "ferdim-p112-"))

    def _cfg(self, **kw):
        cfg = self.env.load_config()
        cfg["max_concurrent"] = 10
        cfg["checksum_sidecar"] = False
        cfg.update(kw)
        return cfg

    def _job(self, url, cfg=None, **kw) -> DownloadJob:
        job = DownloadJob(self.env, cfg or self._cfg(), JobSpec(url=url, **kw))
        self.addCleanup(job.cancel)
        return job

    def _raw_job(self) -> DownloadJob:
        return DownloadJob(self.env, self._cfg(),
                           JobSpec(url="http://127.0.0.1:1/x.bin", segments=1))


# --------------------------------------------------------------- 1) hiz limiti
class SharedSpeedLimitTests(_Base):
    def test_01_queue_bucket_shared_per_job_limit_additive(self):
        env = AppEnv(root=tmp_root(self, "p112-share-"))
        qm = QueueManager(env, env.load_config())
        self.addCleanup(qm.shutdown)
        j1 = qm.add(JobSpec(url="http://127.0.0.1:1/a.bin"), paused=True)
        j2 = qm.add(JobSpec(url="http://127.0.0.1:1/b.bin", limit=54321),
                    paused=True)
        # TEK kuyruk kovasi: iki isin da global kovasi ayni nesne
        self.assertIs(j1._gbucket, qm.bucket)
        self.assertIs(j2._gbucket, qm.bucket)
        self.assertIs(j1._bucket._buckets[0], qm.bucket)
        self.assertIs(j2._bucket._buckets[0], qm.bucket)
        self.assertEqual(j1._job_bucket.rate, 0)
        self.assertEqual(j2._job_bucket.rate, 54321)
        # kuyruk limiti degisince tum isler ANINDA ayni siniri gorur
        qm.set_speed_limit(777_777)
        self.assertEqual(qm.bucket.rate, 777_777)
        self.assertEqual(j1._bucket.rate, 777_777)
        self.assertEqual(j2._bucket.rate, max(777_777, 54321))
        # turbo "limitsiz" der ama is basina --limit yine gecerlidir
        qm.set_turbo(True)
        self.assertEqual(qm.bucket.rate, 0)
        self.assertEqual(j2._job_bucket.rate, 54321)
        self.assertEqual(j2._bucket.rate, 54321)

    def test_02_standalone_job_uses_cfg_limit(self):
        cfg = self._cfg(speed_limit=55_555)
        job = DownloadJob(self.env, cfg, JobSpec(url="http://127.0.0.1:1/y.bin"))
        self.assertEqual(job._gbucket.rate, 55_555)
        self.assertEqual(job._bucket.rate, 55_555)

    def test_03_combined_bucket_requires_all(self):
        g = TokenBucket(0)
        p = TokenBucket(1000)
        cb = CombinedBucket(g, p)
        self.assertEqual(cb.rate, 1000)
        self.assertTrue(cb.take(500))
        stop = threading.Event()
        stop.set()
        self.assertFalse(cb.take(10 ** 9, stop))   # is basina kova engeller
        g.set_rate(2000)
        self.assertEqual(cb.rate, 2000)
        cb.set_rate(0)
        self.assertEqual(cb.rate, 0)
        self.assertTrue(cb.take(10 ** 9, stop))


# ----------------------------------------------------------- 2) kisa 206 / 200
class RangeBehaviourTests(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.short = serve(_Handler, SHORT_DATA)
        cls.ignore = serve(_IgnoreHandler, BIG_DATA)

    @classmethod
    def tearDownClass(cls):
        cls.short.shutdown()
        cls.ignore.shutdown()
        super().tearDownClass()

    def test_01_short_206_is_honoured(self):
        job = self._job(f"{self.short.base}/short.bin", segments=4)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                        f"{job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.final_path.read_bytes(), SHORT_DATA)
        self.assertEqual(hashlib.sha256(job.final_path.read_bytes()).hexdigest(),
                         SHORT_SHA)
        # kisa 206'lar parca parca iner: birden fazla aralik istegi gerekir
        self.assertGreater(self.short.requests, 4)

    def test_02_200_to_ranged_request_degrades_to_single_stream(self):
        job = self._job(f"{self.ignore.base}/big.bin", segments=4)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                        f"{job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.final_path.read_bytes(), BIG_DATA)
        self.assertEqual(hashlib.sha256(job.final_path.read_bytes()).hexdigest(),
                         BIG_SHA)
        self.assertTrue(job.extra.get("no_ranges"))
        self.assertEqual(job.spec.segments, 1)
        self.assertGreaterEqual(self.ignore.ignored, 1)


# ------------------------------------------------------------- 3) Retry-After
def _http_error(headers: dict) -> HTTPError:
    msg = email.message.Message()
    for k, v in headers.items():
        msg[k] = v
    return HTTPError("http://x/f", 429, "limit", msg, None)


class RetryAfterTests(_Base):
    def test_01_numeric_and_clamped(self):
        self.assertEqual(_retry_after(_http_error({"Retry-After": "2"})), 2.0)
        self.assertEqual(_retry_after(_http_error({"Retry-After": "0"})), 1.0)
        self.assertEqual(_retry_after(_http_error({"Retry-After": "99999"})),
                         3600.0)
        self.assertEqual(_retry_after(_http_error({})), 3.0)

    def test_02_http_date_relative_to_server_date(self):
        date = "Wed, 25 Nov 2026 12:00:00 GMT"
        later = "Wed, 25 Nov 2026 12:02:00 GMT"
        got = _retry_after(_http_error({"Retry-After": later, "Date": date}))
        self.assertAlmostEqual(got, 120.0, places=1)
        # gecmis tarih (Date'e gore) 1 sn'ye clamp'lenir
        past = "Wed, 25 Nov 2026 11:00:00 GMT"
        self.assertEqual(
            _retry_after(_http_error({"Retry-After": past, "Date": date})), 1.0)
        # cok uzak tarih 1 saate clamp'lenir
        far = "Fri, 27 Nov 2026 12:00:00 GMT"
        self.assertEqual(
            _retry_after(_http_error({"Retry-After": far, "Date": date})),
            3600.0)

    def test_03_gate_waits_all_workers_and_is_stop_aware(self):
        job = self._raw_job()
        job._retry_gate(5.0)
        self.assertGreater(job._not_before, time.monotonic() + 3.0)
        out = []
        t = threading.Thread(target=lambda: out.append(job._wait_not_before()))
        t.start()
        try:
            time.sleep(0.05)
            job._stop.set()
        finally:
            t.join(2.0)
        self.assertFalse(t.is_alive(), "kapi durdurma sinyalini gormedi")
        self.assertEqual(out, [False])

    def test_04_long_retry_after_sets_job_gate_and_pauses_fast(self):
        srv = serve(_429Handler, BIG_DATA)
        self.addCleanup(srv.shutdown)
        job = self._job(f"{srv.base}/limit.bin", segments=4)
        t0 = time.monotonic()
        job.start()
        self.assertTrue(wait_until(lambda: job._not_before > t0, 15),
                        "429 is geneli kapiyi kurmadi")
        self.assertNotEqual(job.state, JobState.ERROR, job.error)
        job.pause()
        self.assertEqual(job.state, JobState.PAUSED, job.state)


# ------------------------------------------------------- 4) fd tukenmesi/butce
class FdExhaustionTests(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = serve(_Handler, SHORT_DATA)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        super().tearDownClass()

    def test_01_is_fd_error_unwraps_urlerror(self):
        self.assertTrue(job_mod._is_fd_error(OSError(errno.EMFILE, "x")))
        self.assertTrue(job_mod._is_fd_error(OSError(errno.ENFILE, "x")))
        self.assertTrue(job_mod._is_fd_error(
            URLError(OSError(errno.EMFILE, "x"))))
        self.assertFalse(job_mod._is_fd_error(OSError(errno.ECONNRESET, "x")))
        self.assertFalse(job_mod._is_fd_error(RuntimeError("x")))

    def test_02_fd_budget_clamps_ceiling(self):
        if job_mod.resource is None:
            self.skipTest("resource modulu yok")
        with mock.patch.object(job_mod.resource, "getrlimit",
                               return_value=(256, 4096)):
            self.assertEqual(job_mod._fd_budget({"max_concurrent": 4}),
                             (256 - 64) // 4)
            cfg = self._cfg(max_concurrent=4)
            job = DownloadJob(self.env, cfg,
                              JobSpec(url="http://127.0.0.1:1/z.bin",
                                      segments=32))
            self.assertEqual(job._fd_cap, (256 - 64) // 4)
            self.assertLessEqual(job._allowed, job._fd_cap)
            self.assertLessEqual(job._ceiling_locked(), job._fd_cap)

    def test_03_file_open_emfile_pauses_with_code(self):
        job = self._job(f"{self.srv.base}/short.bin", segments=2)
        job._open_part = mock.Mock(
            side_effect=OSError(errno.EMFILE, "Too many open files"))
        job.start()
        snap = job.snapshot()
        self.assertEqual(snap["state"], JobState.PAUSED, snap)
        self.assertEqual(snap["error_code"], "fd_exhausted")

    def test_04_error_code_persists_in_queue_file(self):
        env = AppEnv(root=tmp_root(self, "p112-q-"))
        qm = QueueManager(env, env.load_config())
        self.addCleanup(qm.shutdown)
        job = qm.add(JobSpec(url=f"{self.srv.base}/short.bin", segments=2),
                     paused=True)
        job._open_part = mock.Mock(
            side_effect=OSError(errno.EMFILE, "Too many open files"))
        job.start()
        qm._save()
        recs = json.loads((env.state_dir / "queue.json").read_text())
        rec = next(r for r in recs if r["id"] == job.id)
        self.assertEqual(rec["error_code"], "fd_exhausted")
        self.assertEqual(rec["state"], JobState.PAUSED)


# ------------------------------------------------------------ 5) yer onayi
class AllocationTests(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = serve(_Handler, SHORT_DATA)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        super().tearDownClass()

    def _new_fd(self):
        root = tmp_root(self, "p112-alloc-")
        p = root / "x.bin"
        fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        return fd

    def test_01_allocate_success_and_fallback(self):
        job = self._raw_job()
        fd = self._new_fd()
        job._allocate(fd, 4096)
        self.assertEqual(os.fstat(fd).st_size, 4096)
        fd2 = self._new_fd()
        with mock.patch.object(job_mod.os, "posix_fallocate",
                               side_effect=OSError(errno.EOPNOTSUPP, "yok")):
            job._allocate(fd2, 2048)
        self.assertEqual(os.fstat(fd2).st_size, 2048)   # seyrek dosya

    def test_02_allocate_enospc_raises_for_disk_code(self):
        job = self._raw_job()
        fd = self._new_fd()
        with mock.patch.object(job_mod.os, "posix_fallocate",
                               side_effect=OSError(errno.ENOSPC, "dolu")):
            with self.assertRaises(OSError):
                job._allocate(fd, 4096)

    def test_03_fresh_start_allocates_once(self):
        job = self._job(f"{self.srv.base}/short.bin", segments=2)
        with mock.patch.object(job, "_allocate",
                               wraps=job._allocate) as alloc:
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"{job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        alloc.assert_called_once()
        self.assertEqual(alloc.call_args[0][1], job.total)

    def test_04_resume_does_not_allocate(self):
        url = f"{self.srv.base}/short.bin"
        job = self._job(url, segments=2)
        half = len(SHORT_DATA) // 2
        job.target_dir.mkdir(parents=True, exist_ok=True)
        job.part_path.write_bytes(SHORT_DATA[:half])
        util.atomic_write_json(job.meta_path, {
            "url": url, "etag": SHORT_ETAG, "last_modified": "",
            "ranges": [[half, len(SHORT_DATA) - 1]],
            "done": half, "kind": "http", "total": len(SHORT_DATA),
        })
        with mock.patch.object(job, "_allocate",
                               side_effect=AssertionError("devam allocate etti")):
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"{job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(hashlib.sha256(job.final_path.read_bytes()).hexdigest(),
                         SHORT_SHA)


if __name__ == "__main__":
    unittest.main()
