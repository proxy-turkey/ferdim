"""Kuyruk baslatma dagitimi: yavas hazirlik tick dongusunu kilitlememeli.

Eski hata: QueueManager._tick isleri tick iplikciginde job.start() ile
baslatiyordu; start() icindeki ag yoklamasi (timeout x deneme) yanit
vermeyen bir ana makinede dakikalarca surunce kuyrugun tamami duruyordu:
oteki isler baslamiyor, update_speed()/coexist/kalicilik islemiyordu.

Burada pinlenen davranis: baslatma kisa omurlu bir parcaciga verilir, is
yuvayi HEMEN tutar (max_concurrent asilmaz), tick dongusu donmeye devam
eder ve duraklatma baslatma penceresinde de kazanir.

Testler sabit uyku kullanmaz: kapi (Event) ile olay sirasi kurulur, kalan
beklemeler son tarihli yoklamadir.
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.engine.queue import QueueManager
from ferdim.paths import AppEnv
from tests.helpers import TINY, start_server, tmp_root

# baglanti reddedilir: hazirlik yalniz kapi acilinca ilerler
SLOW_URL = "http://127.0.0.1:1/yavas.bin"


def wait_until(pred, timeout=20.0):
    """Son tarihli yoklama (sabit uyku yok)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


class QueueStartDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    # ------------------------------------------------------------- yardimci
    def _queue(self, **over) -> QueueManager:
        env = AppEnv(root=tmp_root(self, "ferdim-qstart-"))
        cfg = env.load_config()
        cfg.update({"max_concurrent": 2, "smart_queue": False,
                    "retries": 0, "timeout": 2, "schedule": {}})
        cfg.update(over)
        qm = QueueManager(env, cfg)
        self.addCleanup(qm.shutdown)
        return qm

    def _gate(self, match, fail_first=False):
        """Eslesen isin ILK hazirligini kapida tutar (ag gerekmez).

        fail_first=True: kapi acilinca hazirlik baglanti hatasiyla biter
        (erisilemeyen ana makine); aksi halde gercek hazirlik kosar.
        Boylece test gercek kaynak geri cekilme uykularini beklemez."""
        gate, entered = threading.Event(), threading.Event()
        seen = {"n": 0}
        real = DownloadJob._prepare

        def gated(job):
            if match(job):
                seen["n"] += 1
                if seen["n"] == 1:
                    entered.set()
                    gate.wait(30)
                    if fail_first:
                        raise ConnectionError("kapali ana makine")
            real(job)

        patcher = mock.patch.object(DownloadJob, "_prepare", gated)
        patcher.start()
        self.addCleanup(patcher.stop)
        return gate, entered

    def _count_ticks(self) -> dict:
        """Tick dongusunun gercekten dondugunun kaniti: update_speed sayaci."""
        ticks = {"n": 0}
        real = DownloadJob.update_speed

        def counted(job):
            ticks["n"] += 1
            return real(job)

        patcher = mock.patch.object(DownloadJob, "update_speed", counted)
        patcher.start()
        self.addCleanup(patcher.stop)
        return ticks

    # --------------------------------------------------------------- testler
    def test_01_blocked_prepare_does_not_stop_other_jobs(self):
        """Yanit vermeyen ana makine diger isi bekletmemeli: hizli is
        bitene kadar yavas is 'connecting' kalir, tick dongusu doner."""
        qm = self._queue(max_concurrent=2)
        ticks = self._count_ticks()
        gate, entered = self._gate(lambda j: j.url == SLOW_URL,
                                   fail_first=True)
        slow = qm.add(JobSpec(url=SLOW_URL, filename="q-slow.bin",
                              segments=2))
        fast = qm.add(JobSpec(url=f"{self.base}/tiny.bin",
                              filename="q-fast.bin", segments=2))
        self.assertTrue(entered.wait(20), "yavas is hazirliga giremedi")
        self.assertEqual(slow.state, JobState.CONNECTING)
        # tick dongusu donuyor: her tick her is icin update_speed cagirir
        before = ticks["n"]
        self.assertTrue(wait_until(lambda: ticks["n"] >= before + 6, 20),
                        "tick dongusu durdu")
        self.assertEqual(slow.state, JobState.CONNECTING,
                         "yavas is hala hazirlikta olmaliydi")
        self.assertTrue(wait_until(lambda: fast.state == JobState.DONE, 30),
                        f"hizli is bitmedi: {fast.state} {fast.error}")
        self.assertEqual(fast.final_path.read_bytes(), TINY)
        gate.set()
        self.assertTrue(wait_until(
            lambda: slow.state in (JobState.ERROR, JobState.DONE), 30),
            f"yavas is kapanmadi: {slow.state}")
        self.assertEqual(slow.state, JobState.ERROR)

    def test_02_slot_reserved_while_start_is_dispatched(self):
        """Baslatmasi verilmis is yuvayi hemen tutar: eszamanlilik siniri
        (max_concurrent=1) asilmaz, ikinci is kuyrukta bekler."""
        qm = self._queue(max_concurrent=1)
        ticks = self._count_ticks()
        gate, entered = self._gate(lambda j: j.url == SLOW_URL,
                                   fail_first=True)
        slow = qm.add(JobSpec(url=SLOW_URL, filename="q-lim-slow.bin",
                              segments=2))
        fast = qm.add(JobSpec(url=f"{self.base}/tiny.bin",
                              filename="q-lim-fast.bin", segments=2))
        self.assertTrue(entered.wait(20), "yavas is hazirliga giremedi")
        start = ticks["n"]
        self.assertTrue(wait_until(lambda: ticks["n"] >= start + 6, 20),
                        "tick dongusu durdu")
        self.assertEqual(fast.state, JobState.QUEUED,
                         "yuva asildi: ikinci is basladi")
        self.assertIsNone(fast._monitor, "ikinci isin isci havuzu kurulmamali")
        gate.set()
        self.assertTrue(wait_until(lambda: fast.state == JobState.DONE, 30),
                        f"ikinci is baslamadi: {fast.state} {fast.error}")
        self.assertEqual(fast.final_path.read_bytes(), TINY)
        self.assertTrue(wait_until(
            lambda: slow.state in (JobState.ERROR, JobState.DONE), 30))
        self.assertEqual(slow.state, JobState.ERROR, slow.error)
        self.assertTrue(wait_until(lambda: qm._starting == set(), 5),
                        "yuva kaydi bosalmali")

    def test_03_tick_returns_before_prepare_finishes(self):
        """tick, hazirlik bitmeden donmeli (eski hata: ayni iplikte
        job.start() cagriliyordu)."""
        qm = self._queue(max_concurrent=3)
        gate, entered = self._gate(lambda j: j.url == SLOW_URL,
                                   fail_first=True)
        qm._running = False
        for th in (qm._ticker, qm._prober):
            if th is not None:
                th.join(2.0)
        slow = qm.add(JobSpec(url=SLOW_URL, filename="q-tick-slow.bin",
                              segments=2))
        t0 = time.monotonic()
        qm._tick()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2.0, "tick baslatmayi bekledi")
        self.assertTrue(entered.wait(20), "baslatma parcacigi calismadi")
        self.assertEqual(slow.state, JobState.CONNECTING)
        gate.set()
        self.assertTrue(wait_until(
            lambda: slow.state in (JobState.ERROR, JobState.DONE), 30),
            f"yavas is kapanmadi: {slow.state}")

    def test_04_pause_wins_during_async_start(self):
        """Duraklatma, tick'in parcaciga verdigi baslatma penceresinde de
        kazanir: is 'paused' kalir, isci havuzu kurulmaz; devam calisir."""
        qm = self._queue(max_concurrent=2)
        gate, entered = self._gate(
            lambda j: j.final_filename == "q-pause.bin")
        job = qm.add(JobSpec(url=f"{self.base}/tiny.bin",
                             filename="q-pause.bin", segments=2))
        self.assertTrue(entered.wait(20), "is hazirliga giremedi")
        self.assertEqual(job.state, JobState.CONNECTING)
        pauser = threading.Thread(target=job.pause, daemon=True)
        pauser.start()
        self.assertTrue(wait_until(lambda: job._stop.is_set()),
                        "pause dur sinyalini kurmadi")
        gate.set()
        pauser.join(20)
        self.assertFalse(pauser.is_alive(), "pause() donmedi")
        self.assertEqual(job.state, JobState.PAUSED, job.error)
        self.assertIsNone(job._monitor, "isci havuzu kurulmamali")
        self.assertTrue(wait_until(lambda: qm._starting == set(), 5),
                        "yuva kaydi bosalmadi")
        job.resume()                        # kullanici devami hala calisir
        self.assertTrue(wait_until(
            lambda: job.state in (JobState.DONE, JobState.ERROR), 30),
            f"devam etmedi: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.final_path.read_bytes(), TINY)

    def test_05_pause_right_after_dispatch_is_not_lost(self):
        """Dagitimdan HEMEN sonra gelen duraklatma kaybolmamali: is
        'paused' kalir, dosya yazilmaz (start() 'queued' kontrolu ya da
        'connecting' katlamasi karari korur)."""
        qm = self._queue(max_concurrent=2)
        qm._running = False
        for th in (qm._ticker, qm._prober):
            if th is not None:
                th.join(2.0)
        job = qm.add(JobSpec(url=f"{self.base}/tiny.bin",
                             filename="q-dispatch-pause.bin", segments=2))
        self.assertTrue(qm._start_async(job), "baslatma dagitilmadi")
        job.pause()                         # parcacik start()'a girmeden
        self.assertTrue(wait_until(lambda: job.state == JobState.PAUSED, 20),
                        f"duraklatma kayboldu: {job.state} {job.error}")
        self.assertTrue(wait_until(lambda: qm._starting == set(), 5),
                        "yuva kaydi bosalmadi")
        self.assertIsNone(job._monitor, "isci havuzu kurulmamali")
        self.assertEqual(job.done, 0)
        self.assertFalse(job.final_path.exists(), "dosya yazilmamali")


if __name__ == "__main__":
    unittest.main(verbosity=2)
