"""1.13 motor sicak yollari — kilit disi pwrite, heap'li aralik kuyrugu,
olay tabanli isci uyandirma ve token kovasi.

Kapsam:
- `PendingRanges`: en buyuk aralik O(log n) alinir (esit boyda son eklenen
  once), toplam bayt push/pop/clear/bölme boyunca tutarlidir ve diske
  yazilan bicim (liste) degismez,
- `os.pwrite` is kilidi TUTULMADAN yapilir (denetim noktasi/duraklatma
  yazmayi beklemez); bayt muhasebesi yazmadan SONRA kilide girer,
- park etmis isci 0.3 sn yoklamasi yerine iade ile ANINDA uyanir,
- `TokenBucket.take` durdurma sinyalinde <100 ms doner ve stop yokken
  `set_rate` ile erken uyanir,
- yerel HTTP indirmesi segmentli olarak bayt bayt aynidir; ortada
  duraklat/devam veri kaybetmez,
- yayinlama penceresinde ad yer tutucusu birakilmaz (baska is ayni adi
  kapamaz) ve hata yolu yer tutucuyu sizdirmaz.
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import util
from ferdim.engine import job as job_mod
from ferdim.engine.job import (DownloadJob, JobSpec, JobState, PendingRanges)
from ferdim.paths import AppEnv
from ferdim.ratelimit import CombinedBucket, TokenBucket
from tests.helpers import start_server, tmp_root

DATA = os.urandom(24 * 1024 * 1024 + 12345)   # 24 MB + tuhaf kuyruk


def wait_until(pred, timeout=60.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def wait_state(job, states, timeout=60):
    return wait_until(lambda: job.state in states, timeout)


class _PacedHandler(BaseHTTPRequestHandler):
    """Range destekli, yavas (baglanti basi ~1.6 MB/sn) yayin."""
    protocol_version = "HTTP/1.1"
    payload = b""
    block = 32 * 1024
    delay = 0.02

    def log_message(self, *a):
        pass

    def do_GET(self):
        data = self.payload
        total = len(data)
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, b = rng[6:].split("-", 1)
            a = int(a)
            b = min(int(b) if b else total - 1, total - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{total}")
        else:
            a, b = 0, total - 1
            self.send_response(200)
        self.send_header("Content-Length", str(b - a + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"p113"')
        self.end_headers()
        pos = a
        try:
            while pos <= b:
                end = min(pos + self.block, b + 1)
                self.wfile.write(data[pos:end])
                self.wfile.flush()
                pos = end
                time.sleep(self.delay)
        except (BrokenPipeError, ConnectionResetError):
            pass


class PendingRangesTests(unittest.TestCase):
    def test_01_largest_first_and_stable_ties(self):
        p = PendingRanges()
        p.push(0, 99)            # 100 bayt
        p.push(200, 299)         # 100 bayt (sonra eklendi: esitlikte once)
        p.push(400, 449)         # 50 bayt
        self.assertEqual(len(p), 3)
        self.assertEqual(p.total, 250)
        self.assertEqual(p.pop_largest(), (200, 299))
        self.assertEqual(p.pop_largest(), (0, 99))
        self.assertEqual(p.pop_largest(), (400, 449))
        self.assertIsNone(p.pop_largest())
        self.assertEqual(p.total, 0)
        self.assertEqual(len(p), 0)

    def test_02_total_consistent_through_push_clear_and_split(self):
        p = PendingRanges([(0, 1023), (2048, 3071)])
        self.assertEqual(p.total, 2048)
        self.assertEqual(len(p), 2)
        p.push(4096, 4096)
        self.assertEqual(p.total, 2049)
        self.assertEqual(list(p), [(0, 1023), (2048, 3071), (4096, 4096)])
        self.assertIn((2048, 3071), p)
        self.assertNotIn((1, 2), p)
        self.assertEqual(p, [(0, 1023), (2048, 3071), (4096, 4096)])
        p.clear()
        self.assertEqual((len(p), p.total, p.to_list()), (0, 0, []))

    def test_03_job_split_keeps_total_and_old_list_view(self):
        env = AppEnv(root=tmp_root(self, "ferdim-p113-split-"))
        job = DownloadJob(env, env.load_config(),
                          JobSpec(url="http://x/f.bin", segments=4))
        self.addCleanup(job.cancel)
        job.total = 3 << 20
        job._min_split = 1 << 20
        job._gov["best"] = 0.0
        job._pending = [(0, (3 << 20) - 1)]
        self.assertEqual(job._pending.total, 3 << 20)
        rng = job._take_range()
        self.assertIsNotNone(rng)
        taken = rng[1] - rng[0] + 1
        # kuyrukta kalan + alinan = basta toplam (kayip/cift sayim yok)
        self.assertEqual(job._pending.total + taken, 3 << 20)
        self.assertEqual(job._pending_bytes_locked(), job._pending.total)
        self.assertEqual(len(job._pending), 1)
        job._pending = []                 # eski liste atamasi hala calisir
        self.assertEqual(job._pending, [])
        self.assertEqual(job._pending.total, 0)


class PwriteLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_01_pwrite_blocks_outside_job_lock(self):
        """pwrite surerken `_lock` baska thread'den alinabilmeli: denetim
        noktasi/duraklatma yazmayi beklemez."""
        env = AppEnv(root=tmp_root(self, "ferdim-p113-write-"))
        job = DownloadJob(env, env.load_config(),
                          JobSpec(url=f"{self.base}/file.bin", segments=4))
        self.addCleanup(job.cancel)
        entered, release = threading.Event(), threading.Event()
        real = os.pwrite

        def slow_pwrite(fd, data, off):
            if not entered.is_set():
                entered.set()
                release.wait(15)
            return real(fd, data, off)

        with mock.patch.object(job_mod.os, "pwrite", side_effect=slow_pwrite):
            job.start()
            self.assertTrue(entered.wait(20), "pwrite hic cagrilmadi")
            got = job._lock.acquire(timeout=0.3)
            try:
                self.assertTrue(got, "pwrite is kilidini tutuyor")
            finally:
                if got:
                    job._lock.release()
            release.set()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR), 60),
                            f"{job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)


class ParkedWorkerTests(unittest.TestCase):
    def test_01_requeue_wakes_parked_worker_immediately(self):
        """Park etmis isci 0.3 sn yoklamaz: aralik iadesi notify eder."""
        env = AppEnv(root=tmp_root(self, "ferdim-p113-park-"))
        job = DownloadJob(env, env.load_config(),
                          JobSpec(url="http://x/f.bin", segments=4))
        self.addCleanup(job.cancel)
        job.kind = "http"
        job.state = JobState.DOWNLOADING
        job.total = 1 << 20
        job._allowed = 4
        job._active = 1            # bir slot baska iscide: bu parkta
        got = []

        def worker():
            t0 = time.monotonic()
            rng = job._acquire(0)
            got.append((rng, time.monotonic() - t0))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.15)          # parkta beklediginden emin ol
        self.assertEqual(got, [])
        with job._lock:
            job._push_pending_locked(0, 4095)
        t0 = time.monotonic()
        t.join(5.0)
        self.assertFalse(t.is_alive(), "park etmis isci uyanmadi")
        self.assertEqual(got[0][0], (0, 4095))
        self.assertLess(time.monotonic() - t0, 0.25,
                        "uyandirma yoklama gecikmesi tasiyor")
        self.assertLess(got[0][1], 5.0)


class TokenBucketTests(unittest.TestCase):
    def test_01_stop_returns_false_promptly(self):
        b = TokenBucket(1000)                 # 1 KB/sn: 10 MB asla sigmaz
        stop = threading.Event()
        out = []
        t = threading.Thread(target=lambda: out.append(b.take(10_000_000, stop)))
        t.start()
        time.sleep(0.05)
        t0 = time.monotonic()
        stop.set()
        t.join(2.0)
        self.assertFalse(t.is_alive(), "durdurma beklemesi kesilmedi")
        self.assertLess(time.monotonic() - t0, 0.1,
                        "stop 100 ms icinde donmedi")
        self.assertEqual(out, [False])         # bayt harcanmadi
        self.assertTrue(b.take(1))             # kova kullanilabilir

    def test_02_set_rate_wakes_waiter_early(self):
        b = TokenBucket(1000)
        out = []
        t = threading.Thread(target=lambda: out.append(b.take(1 << 20)))
        t.start()
        time.sleep(0.05)
        t0 = time.monotonic()
        b.set_rate(1 << 30)                    # ~0.05 sn'lik birikim yeter
        t.join(2.0)
        self.assertFalse(t.is_alive(), "set_rate bekleyeni uyandirmadi")
        self.assertLess(time.monotonic() - t0, 0.2)
        self.assertEqual(out, [True])

    def test_03_combined_bucket_refunds_on_stop(self):
        fast = TokenBucket(10 ** 9)
        slow = TokenBucket(1000)
        cb = CombinedBucket(fast, slow)
        before = fast._tokens
        stop = threading.Event()
        out = []
        t = threading.Thread(target=lambda: out.append(cb.take(10 ** 6, stop)))
        t.start()
        time.sleep(0.05)
        stop.set()
        t.join(2.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(out, [False])
        self.assertEqual(fast._tokens, before)  # hizli kovadan sizmadi


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _PacedHandler)
        cls.srv.daemon_threads = True
        _PacedHandler.payload = DATA
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def _job(self, name="f.bin", segments=8):
        env = AppEnv(root=tmp_root(self, "ferdim-p113-e2e-"))
        cfg = env.load_config()
        cfg["checksum_sidecar"] = False
        cfg["learn_hosts"] = False
        job = DownloadJob(env, cfg, JobSpec(url=f"{self.base}/{name}",
                                            segments=segments))
        self.addCleanup(job.cancel)
        return job

    def test_01_segmented_download_byte_identical(self):
        job = self._job(segments=8)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR), 120),
                        f"{job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.done, job.total)
        self.assertEqual(hashlib.sha256(job.final_path.read_bytes()).hexdigest(),
                         hashlib.sha256(DATA).hexdigest())
        self.assertEqual(job._pending.total, 0)
        self.assertFalse(list(job.target_dir.glob(".ferdim-*.part")),
                         "yarim .part kaldi")

    def test_02_pause_midway_then_resume_is_correct(self):
        job = self._job(segments=8)
        job.start()
        self.assertTrue(wait_until(
            lambda: job.state == JobState.DOWNLOADING and job.done > 0), 30)
        self.assertTrue(wait_until(
            lambda: job.done > job.total // 4 or job.state != JobState.DOWNLOADING,
            60), f"{job.state} {job.done}/{job.total}")
        job.pause()
        self.assertEqual(job.state, JobState.PAUSED, job.error)
        self.assertEqual(job._inflight, {}, "duraklatmada ucustaki aralik kaldi")
        self.assertTrue(job.meta_path.exists(), "denetim noktasi yazilmadi")
        done_at_pause = job.done
        self.assertGreater(done_at_pause, 0)
        job.resume()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR), 120),
                        f"{job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.done, job.total)
        self.assertEqual(hashlib.sha256(job.final_path.read_bytes()).hexdigest(),
                         hashlib.sha256(DATA).hexdigest())


class ReservationTests(unittest.TestCase):
    def _job(self):
        env = AppEnv(root=tmp_root(self, "ferdim-p113-res-"))
        job = DownloadJob(env, env.load_config(),
                          JobSpec(url="http://x/f.bin", segments=2))
        self.addCleanup(job.cancel)
        job.target_dir.mkdir(parents=True, exist_ok=True)
        return job

    def test_01_publishing_keeps_placeholder(self):
        """Yayin penceresinde `_drop_reservation` adi BIRAKMAZ: arada baska
        bir is ayni adi kapip iki yayin birbirini ezemez."""
        job = self._job()
        dest = util.unique_path(job.target_dir, "ad.bin")
        with job._lock:
            job._reserved = dest
            job._publishing = True
        job._drop_reservation()
        self.assertTrue(dest.exists(), "yayin penceresinde yer tutucu silindi")
        with job._lock:
            job._publishing = False
        job._drop_reservation()
        self.assertFalse(dest.exists(), "yer tutucu birakilmadi")
        self.assertIsNone(job._reserved)

    def test_02_failed_publish_leaks_no_placeholder(self):
        """os.replace basarisiz olursa ad yer tutucusu sizmaz."""
        job = self._job()
        job.state = JobState.DOWNLOADING
        job.total = 4096
        job.done = 4096
        job._finalize_file()              # .part yok: replace hata verir
        self.assertEqual(job.state, JobState.ERROR)
        leftovers = [p.name for p in job.target_dir.iterdir()
                     if not p.name.startswith(".ferdim-")]
        self.assertEqual(leftovers, [], f"yer tutucu sizdi: {leftovers}")


if __name__ == "__main__":
    unittest.main()
