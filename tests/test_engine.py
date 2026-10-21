"""Motor testleri: segmentli indirme, resume, restart, no-range, HLS, auth, CD."""
from __future__ import annotations

import hashlib
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.paths import AppEnv
from tests.helpers import DATA, DATA_SHA, SEG, start_server


def wait_state(job, states, timeout=30):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if job.state in states:
            return True
        time.sleep(0.1)
    return False


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()
        cls.env = AppEnv(root=Path("/tmp/ferdim-test-env"))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _job(self, url, **kw) -> DownloadJob:
        cfg = self.env.load_config()
        cfg["max_concurrent"] = 10
        spec = JobSpec(url=url, **kw)
        return DownloadJob(self.env, cfg, spec)

    def test_01_segmented_download(self):
        job = self._job(f"{self.base}/file.bin", segments=8)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                        f"suren durum: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.total, len(DATA))
        self.assertEqual(sha(job.final_path), DATA_SHA)
        self.assertEqual(job.final_filename, "dosya adı.bin")  # CD'den

    def test_02_pause_resume(self):
        job = self._job(f"{self.base}/slow.bin", segments=4)
        job.start()
        t0 = time.monotonic()
        while job.done < 200_000 and time.monotonic() - t0 < 20:
            time.sleep(0.05)
        job.pause()
        self.assertTrue(wait_state(job, (JobState.PAUSED,)), "pause olmadi")
        mid = job.done
        self.assertGreater(mid, 0)
        time.sleep(1.0)
        self.assertEqual(job.done, mid, "pause sonrasi ilerleme var!")
        job.resume()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                        f"resume: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path), DATA_SHA)

    def test_03_restart_on_etag_change(self):
        job = self._job(f"{self.base}/file.bin", segments=4)
        job.start()
        t0 = time.monotonic()
        while job.done < 300_000 and time.monotonic() - t0 < 20:
            time.sleep(0.05)
        job.pause()
        self.assertTrue(wait_state(job, (JobState.PAUSED,)))
        # simdi ayni URL farkli ETag donduruyor (file2)
        job.url = f"{self.base}/file2.bin"
        job.resume()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR), 40),
                        f"restart: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path), DATA_SHA)

    def test_04_no_range_single_stream(self):
        job = self._job(f"{self.base}/norange.bin", segments=8)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.final_path.stat().st_size, 1024 * 1024)

    def test_05_hls(self):
        job = self._job(f"{self.base}/master.m3u8", segments=4)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR), 60),
                        f"hls: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.final_path.read_bytes(), b"".join(SEG))

    def test_06_auth(self):
        job = self._job(f"{self.base}/auth", user="u", password="p")
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.final_path.read_bytes(), b"OK")

    def test_07_redirect(self):
        job = self._job(f"{self.base}/redir", segments=2)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path), DATA_SHA)

    def test_08_cancel(self):
        job = self._job(f"{self.base}/slow.bin", segments=2,
                        filename="cancel-test.bin")
        job.start()
        t0 = time.monotonic()
        while job.done < 100_000 and time.monotonic() - t0 < 20 \
                and job.state == JobState.DOWNLOADING:
            time.sleep(0.05)
        job.cancel(delete_files=True)
        self.assertEqual(job.state, JobState.CANCELED)
        self.assertFalse(job.part_path.exists(), "part dosyasi silinmedi")
        self.assertFalse(
            (self.env.download_dir / "cancel-test.bin").exists(),
            "iptal edilen indirme dosyasi olusmamali")
    def test_10_governor_integrity(self):
        """Isci basi kisitli sunucu: butunluk korunmali, is hata ile
        bitmemeli. (Buyume kararini test_governor.py deterministik dogrular.)"""
        job = self._job(f"{self.base}/perconn.bin", segments=6)
        job._allowed = 2          # onceki sunucu cezasindan kalmis gibi basla
        job.start()
        self.assertTrue(
            wait_state(job, (JobState.DONE, JobState.ERROR), 180),
            f"suren durum: {job.state}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path), DATA_SHA)
        self.assertGreaterEqual(job._allowed, 1)

    def test_09_adaptive_connection_limit(self):
        """Sunucu 3 eszamanli baglantiya izin veriyor: is hata ile bitmemeli,
        havuz kuculup indirme tamamlanmali."""
        job = self._job(f"{self.base}/limit3.bin", segments=16)
        job.start()
        self.assertTrue(
            wait_state(job, (JobState.DONE, JobState.ERROR), 180),
            f"suren durum: {job.state}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path),
                         hashlib.sha256(DATA).hexdigest())
        self.assertLessEqual(job._allowed, 3)
        self.assertGreaterEqual(job._allowed, 1)
        snap = job.snapshot()
        self.assertLessEqual(snap["segments_effective"], 3)
        self.assertIsNotNone(snap["adaptive"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
