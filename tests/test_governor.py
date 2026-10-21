"""Governor (verimlilik valisi) karar mantigi — deterministik saat ile.

Ag/CPU hizindan bagimsiz: _governor_tick dogrudan sahte zaman ve yapay
ilerleme ile calistirilir; yukari kesif kabul/red ve asagi verimlilik
kesifi senaryolari dogrulanir.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ferdim.engine.job as jobmod
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.paths import AppEnv


class FakeTime:
    def __init__(self, start=1000.0):
        self.t = start

    def monotonic(self):
        return self.t

    def time(self):
        return 1_790_000_000.0

    def sleep(self, _x):
        self.t += 0.01


def make_job(segments=6, allowed=2):
    env = AppEnv(root=Path(tempfile.mkdtemp(prefix="ferdim-gov-")))
    cfg = env.load_config()
    job = DownloadJob(env, cfg, JobSpec(url="http://x/f.bin",
                                        segments=segments))
    # ag olmadan, dogrudan gov observed bir is simule et
    job.kind = "http"
    job.state = JobState.DOWNLOADING
    job.total = 100_000_000
    job._pending = [(0, 99_999_999)]
    job._allowed = allowed
    job._seg_max = segments
    job._gov["t"] = jobmod.time.monotonic()
    job._gov["done"] = job.done
    return job


class GovernorTests(unittest.TestCase):
    def setUp(self):
        self.ft = FakeTime()
        self.real_time = jobmod.time
        jobmod.time = self.ft

    def tearDown(self):
        jobmod.time = self.real_time

    def _tick(self, job, bytes_per_sec, dt=2.6):
        self.ft.t += dt
        job.done += int(bytes_per_sec * dt)
        job._governor_tick()

    def test_up_probe_accepted(self):
        """Hiz artiyorsa +1 baglanti kabul edilir ve tirmanma planlanir."""
        job = make_job(segments=6, allowed=2)
        self._tick(job, 1_000_000)            # olcum + ilk yukari kesif
        self.assertEqual(job._allowed, 3)     # probe: 2 -> 3
        self._tick(job, 3_000_000)            # 3 baglantiyla hiz 3x -> kabul
        self.assertEqual(job._allowed, 3)
        self.assertEqual(job._gov["best_workers"], 3)
        # EMA gecikmelidir: 1MB/s -> 3MB/s adiminda 1.8MB/s civarina cikar
        self.assertGreater(job._gov["best"], 1_600_000)
        self.assertLess(job._gov["best"], 3_200_000)
        # kabul sonrasi tirmanma kisa aralikla planlanir
        self.assertLessEqual(job._gov["next_up"], self.ft.t + 5.1)

    def test_up_probe_rejected_reverts(self):
        """Hiz belirgin dustuyse kesif geri alinir, cooldown konur."""
        job = make_job(segments=6, allowed=2)
        self._tick(job, 2_000_000)            # probe -> 3, ref=2MB/s
        self.assertEqual(job._allowed, 3)
        self._tick(job, 200_000)              # ciddi kayip -> geri al
        self.assertEqual(job._allowed, 2)
        self.assertGreater(job._gov["cooldown"], self.ft.t)

    def test_down_probe_releases_surplus(self):
        """Hiz tavanda sabitken fazla baglanti birakilir."""
        job = make_job(segments=6, allowed=6)
        job._gov["best"] = 4_000_000
        job._gov["best_workers"] = 6
        job._gov["next_down"] = 0.0
        job._gov["next_up"] = self.ft.t + 3600   # yukari kesifi izole et
        self._tick(job, 4_000_000)             # ema ~= best, pending devasa
        self.assertEqual(job._allowed, 5)      # asagi kesif aktif
        self.assertTrue(job._gov["probe"])
        self.assertEqual(job._gov["dir"], -1)
        self._tick(job, 4_000_000)             # hiz korundu -> kabul
        self.assertEqual(job._allowed, 5)
        self.assertEqual(job._gov["best_workers"], 5)
        self.assertFalse(job._gov["probe"])

    def test_down_probe_respects_server_floor(self):
        """Sunucu cezasi tabaninin altina inilmez."""
        job = make_job(segments=6, allowed=3)
        job._server_floor = 3                  # ceza tabani 3
        job._gov["best"] = 4_000_000
        job._gov["next_down"] = 0.0
        job._gov["next_up"] = self.ft.t + 3600
        self._tick(job, 4_000_000)
        self.assertEqual(job._allowed, 3)      # asagi kesif yapilmadi

    def test_limit_penalty_blocks_growth(self):
        """429/503 cezasi penceresinde yukari kesif yapilmaz."""
        job = make_job(segments=6, allowed=2)
        job._last_limit_ts = self.ft.t         # hemen simdi ceza
        job._gov["next_up"] = 0.0
        self._tick(job, 1_000_000)
        self.assertEqual(job._allowed, 2)      # buyume engellendi

    def test_shrink_sets_floor_and_penalty(self):
        job = make_job(segments=6, allowed=8)
        job._shrink("HTTP 503")
        self.assertEqual(job._allowed, 4)
        self.assertEqual(job._server_floor, 4)
        self.assertGreater(job._last_limit_ts, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
