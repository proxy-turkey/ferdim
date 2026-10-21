"""v1.4.0 ozellik testleri: DASH, ayna karisimi, saglama, metalink,
ZIP onizleme, akilli kuyruk."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import metalink, util
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.engine.queue import QueueManager
from ferdim.paths import AppEnv
from tests.helpers import (DASH_INIT, DASH_SEGS, DATA, DATA_SHA, REQUEST_COUNTS,
                           start_server)


def wait_state(job, states, timeout=60):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if job.state in states:
            return True
        time.sleep(0.1)
    return False


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv_a, cls.base_a = start_server()
        cls.env = AppEnv(root=Path(tempfile.mkdtemp(prefix="ferdim-feat-")))
        cls.cfg = cls.env.load_config()
        cls.cfg["max_concurrent"] = 10

    @classmethod
    def tearDownClass(cls):
        cls.srv_a.shutdown()

    def _job(self, url, **kw):
        return DownloadJob(self.env, self.cfg, JobSpec(url=url, **kw))

    # ------------------------------------------------------------ saglama
    def test_01_checksum_ok(self):
        job = self._job(f"{self.base_a}/file.bin", segments=4,
                        sha256=DATA_SHA)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.snapshot()["checksum"], "ok")

    def test_02_checksum_mismatch(self):
        job = self._job(f"{self.base_a}/file.bin", segments=2,
                        sha256="0" * 64)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)))
        self.assertEqual(job.state, JobState.ERROR)
        self.assertIn("saglama", job.error)
        self.assertEqual(job.snapshot()["checksum"], "mismatch")
        job.cancel(delete_files=True)

    # ---------------------------------------------------------- ayna karisimi
    def test_03_mirror_blending(self):
        srv_b, base_b = start_server()
        try:
            job = self._job(f"{self.base_a}/file.bin", segments=8,
                            mirrors=[f"{base_b}/file.bin"])
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)))
            self.assertEqual(job.state, JobState.DONE, job.error)
            self.assertEqual(sha(job.final_path), DATA_SHA)
            # her iki sunucudan da byte cekilmeli
            self.assertGreater(REQUEST_COUNTS.get(self.srv_a.server_address[1],
                                                  0), 1)
            self.assertGreater(REQUEST_COUNTS.get(srv_b.server_address[1], 0), 1)
        finally:
            srv_b.shutdown()

    # ---------------------------------------------------------------- DASH
    def test_04_dash_mpd(self):
        job = self._job(f"{self.base_a}/manifest.mpd", segments=4)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR), 90),
                        f"{job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.final_path.read_bytes(),
                         DASH_INIT + b"".join(DASH_SEGS))

    # ------------------------------------------------------------ metalink
    def test_05_metalink_parse(self):
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<metalink version="3.0" xmlns="urn:ietf:params:xml:ns:metalink">
 <file name="distro.iso">
  <size>{len(DATA)}</size>
  <hash type="sha-256">{DATA_SHA}</hash>
  <url preference="90">https://mirror-a.test/distro.iso</url>
  <url preference="10">https://mirror-b.test/distro.iso</url>
 </file>
</metalink>"""
        items = metalink.parse(xml)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["filename"], "distro.iso")
        self.assertEqual(it["sha256"], DATA_SHA)
        # dusuk preference degeri (10) once gelir
        self.assertEqual(it["urls"][0], "https://mirror-b.test/distro.iso")
        self.assertTrue(metalink.looks_like_metalink("http://x/f.meta4"))
        self.assertFalse(metalink.looks_like_metalink("http://x/f.bin"))

    # --------------------------------------------------------- ZIP onizleme
    def test_06_zip_preview(self):
        with tempfile.TemporaryDirectory() as d:
            zp = Path(d) / "a.zip"
            with zipfile.ZipFile(zp, "w") as zf:
                zf.writestr("klasor/abc.txt", b"x" * 100)
                zf.writestr("def.bin", b"y" * 4096)
            entries, note = util.zip_preview(zp)
            self.assertEqual(note, "")
            self.assertEqual({e[0] for e in entries},
                             {"klasor/abc.txt", "def.bin"})
            bad, msg = util.zip_preview(Path(d) / "yok.zip")
            self.assertIsNone(bad)
            self.assertTrue(msg)

    # -------------------------------------------------------- akilli kuyruk
    def test_07_smart_queue_small_first(self):
        cfg = self.env.load_config()
        cfg["max_concurrent"] = 1
        cfg["smart_queue"] = True
        qm = QueueManager(self.env, cfg)
        try:
            big = qm.add(JobSpec(url=f"{self.base_a}/slow.bin",
                                 filename="smart-big.bin"))
            small = qm.add(JobSpec(url=f"{self.base_a}/tiny.bin",
                                   filename="smart-tiny.bin"))
            deadline = time.monotonic() + 25
            started = None
            while time.monotonic() < deadline:
                if big.state == JobState.DOWNLOADING or \
                   small.state == JobState.DOWNLOADING:
                    started = small.id if small.state == \
                        JobState.DOWNLOADING else big.id
                    break
                time.sleep(0.1)
            self.assertEqual(started, small.id,
                             "kucuk dosya once baslamaliydi")
            self.assertTrue(wait_state(small, (JobState.DONE,), 30))
            big.cancel(delete_files=True)
        finally:
            qm.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
