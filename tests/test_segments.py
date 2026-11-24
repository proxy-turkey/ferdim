"""HLS/DASH segment havuzu ogrenmesi (ROADMAP 1.7).

Kapsam: havuz isci sayisi hesabi (tohum/tavan kirpma), ogrenmenin
profil deposuna yazilmasi (bitis/duraklatma, 429/503) ve isin ogrenilmis
sayidan baslamasi. Ag testleri yerel Range sunucusuna karsi calisir;
duraklatma yolu sahte stop olayi ile deterministik tutulur.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ferdim.engine.segments as segmod
from ferdim.engine import httpcli, segments
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.paths import AppEnv
from ferdim.profiles import open_profiles
from ferdim.ratelimit import TokenBucket
from tests.helpers import SEG, start_server, tmp_root

SEG_MIN = segmod.SEG_LEARN_MIN


def wait_state(job, states, timeout=30):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if job.state in states:
            return True
        time.sleep(0.1)
    return False


class PoolSizeTests(unittest.TestCase):
    def test_default_is_half_the_segments(self):
        self.assertEqual(segments.pool_size(8), 4)      # eski davranis
        self.assertEqual(segments.pool_size(16), 8)     # tavan 8
        self.assertEqual(segments.pool_size(3), 1)
        self.assertEqual(segments.pool_size(1), 1)
        self.assertEqual(segments.pool_size(0), 1)

    def test_seed_wins_and_is_clamped(self):
        self.assertEqual(segments.pool_size(8, seed=6), 6)
        self.assertEqual(segments.pool_size(8, seed=99), 8)   # tavan
        self.assertEqual(segments.pool_size(8, seed=4, cap=2), 2)
        self.assertEqual(segments.pool_size(8, seed=4, cap=0), 4)
        self.assertEqual(segments.pool_size(2, seed=6), 2)    # segment sayisi
        self.assertEqual(segments.pool_size(8, seed="bozuk"), 4)

    def test_cap_cannot_exceed_the_fixed_maximum(self):
        self.assertEqual(segments.pool_size(32, seed=32, cap=32),
                         segments.SEG_MAX)


class _FlakyHandler(BaseHTTPRequestHandler):
    """429/503 donduren segment sunucusu: limit sinyali taklidi."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(503)
        self.send_header("Content-Length", "0")
        self.end_headers()


class AssembleLearnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.env = AppEnv(root=tmp_root(self, "ferdim-seg-"))
        self.opt = httpcli.DlOptions(user_agent="test", timeout=10)
        self.bucket = TokenBucket(0)

    def _run(self, n=len(SEG), store=None, stop=None, rtt=0.03, cap=8):
        meta = {}
        out = self.env.state_dir / "out.bin"
        fd = os.open(out, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            segments.assemble(
                self.opt, fd, meta, stop or threading.Event(), self.bucket,
                None, self.env.state_dir / "meta.json", None,
                [f"{self.base}/seg{i:03d}.ts" for i in range(n)],
                store=store, cap=cap, rtt=rtt)
        finally:
            os.close(fd)
        return meta

    def test_done_records_pool_size_and_rtt(self):
        store = open_profiles(self.env)
        meta = self._run(store=store, rtt=0.03)
        self.assertEqual(meta["segments_total"], len(SEG))
        e = store.get(self.base)
        self.assertIsNotNone(e, "biten havuz profili yazilmali")
        self.assertEqual(e["seg_workers"], segments.pool_size(len(SEG)))
        self.assertEqual(e["rtt_band"], "low")
        self.assertEqual(e["links"], 0, "segment isi aralik tohumunu ezmez")
        self.assertEqual(e["samples"], 1)

    def test_seed_from_profile_drives_the_pool(self):
        """Profilde 2 yaziliysa havuz 2 isciyle kosar (ortalama 2 kalir)."""
        store = open_profiles(self.env)
        store.record(self.base, seg_workers=2)
        self._run(store=store)
        e = store.get(self.base)
        self.assertEqual(e["seg_workers"], 2,
                         "tohum okunmadi: varsayilan 4 ile ortalama 3 olurdu")
        self.assertEqual(e["samples"], 2)

    def test_short_paused_run_is_not_learned(self):
        """Duraklatilan kosuda hic segment inmediyse ogrenecek sey yok."""
        store = open_profiles(self.env)
        stop = threading.Event()
        stop.set()
        self._run(store=store, stop=stop)
        self.assertIsNone(store.get(self.base))

    def test_no_store_learns_nothing(self):
        self._run(store=None)
        self.assertFalse((self.env.state_dir / "hosts.json").exists())

    def test_fetch_all_honours_stop_under_speed_limit(self):
        """Hiz kovasinda bekleyen segment okumasi durunca hemen doner ve
        YARIM veri dondurmez (HLS/DASH duraklamasi senkron kalsin)."""
        stop = threading.Event()
        bucket = TokenBucket(1024)          # 1 KB/s: ilk blok ~1 dk bekler
        out: dict = {}

        def run():
            out["data"] = segments.fetch_all(
                f"{self.base}/file.bin", self.opt, bucket, stop)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        time.sleep(0.2)
        stop.set()
        t.join(3.0)
        self.assertFalse(t.is_alive(), "hiz kovasindaki okuma durmadi")
        self.assertIsNone(out.get("data"), "duran okuma yarim veri dondurdu")

    def test_all_throttled_run_learns_nothing(self):
        """Hic segment inemediyse (hepsi 503) ogrenecek olcum yok."""
        store = open_profiles(self.env)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _FlakyHandler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        meta = {}
        fd = os.open(self.env.state_dir / "out2.bin",
                     os.O_RDWR | os.O_CREAT, 0o644)
        try:
            with self.assertRaises(RuntimeError):
                segments.assemble(
                    self.opt, fd, meta, threading.Event(), self.bucket, None,
                    self.env.state_dir / "meta2.json", None,
                    [f"{base}/seg{i:03d}.ts" for i in range(8)],
                    store=store)
        finally:
            os.close(fd)
        self.assertIsNone(store.get(base))

    def test_learn_policy_directly(self):
        store = open_profiles(self.env)
        segments._learn(store, "cdn.test", 6, SEG_MIN - 1, False, 0.02)
        self.assertIsNone(store.get("cdn.test"))
        segments._learn(store, "cdn.test", 6, SEG_MIN, False, 0.02)
        self.assertEqual(store.get("cdn.test")["seg_workers"], 6)
        segments._learn(None, "cdn.test", 6, 99, False, 0.02)
        self.assertEqual(store.get("cdn.test")["samples"], 1)
        # 429/503: yarisi bildirilir, throttle isareti kalici
        segments._learn(store, "cdn.test", 6, 8, True, 0.02)
        e = store.get("cdn.test")
        self.assertTrue(e["throttled"])
        self.assertEqual(e["seg_workers"], int(round((6 + 3) / 2)))


class HlsJobProfileTests(unittest.TestCase):
    """Is uctan uca: HLS/DASH isi havuz sayisini ogrenir ve tohumlar."""

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.env = AppEnv(root=tmp_root(self, "ferdim-seg-job-"))
        self.cfg = self.env.load_config()

    def _job(self, url, **kw) -> DownloadJob:
        return DownloadJob(self.env, self.cfg, JobSpec(url=url, **kw))

    def _run(self, path):
        job = self._job(f"{self.base}{path}")
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                        f"{job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        return job

    def test_hls_done_learns_pool_size(self):
        job = self._run("/media.m3u8")
        e = open_profiles(self.env).get(self.base)
        self.assertIsNotNone(e, "HLS isi segment havuzunu ogrenmeli")
        self.assertEqual(e["seg_workers"], segments.pool_size(len(SEG)))
        self.assertEqual(e["links"], 0)
        self.assertIn(e["rtt_band"], ("low", "mid", "high"))

    def test_hls_job_seeds_from_learned_pool_size(self):
        store = open_profiles(self.env)
        store.record(self.base, seg_workers=2)
        self._run("/media.m3u8")
        self.assertEqual(store.get(self.base)["seg_workers"], 2,
                         "is ogrenilmis havuz sayisindan baslamali")

    def test_dash_done_learns_pool_size(self):
        self._run("/manifest.mpd")
        e = open_profiles(self.env).get(self.base)
        self.assertIsNotNone(e, "DASH isi segment havuzunu ogrenmeli")
        self.assertEqual(e["seg_workers"], segments.pool_size(6))
        self.assertIn(e["rtt_band"], ("low", "mid", "high"))

    def test_learn_hosts_false_disables_segment_learning(self):
        self.cfg["learn_hosts"] = False
        self._run("/media.m3u8")
        self.assertIsNone(open_profiles(self.env).get(self.base))

    def test_seg_cap_follows_external_cap(self):
        """Birlikte yasam tavani segment havuzunu da kisitlar (1..8)."""
        job = self._job(f"{self.base}/media.m3u8")
        job.kind = "hls"
        self.assertEqual(job._seg_cap(), segments.SEG_MAX)
        job.set_external_cap(2)
        self.assertEqual(job._seg_cap(), 2)
        job.set_external_cap(1)
        self.assertEqual(job._seg_cap(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
