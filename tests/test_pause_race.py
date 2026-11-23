"""Duraklatma/iptal, baslatma penceresiyle YARISIRKEN de kesin olmali.

Eski hata: start() once durumu 'downloading' yapiyor, monitor'u ancak
_prepare() sonrasi yayinliyordu. Tam bu arada gelen pause() yayinlanmamis
(None ya da onceki kosunun olu) monitor'u join edip 'downloading' durumda
donuyordu; pause GUI/CLI'da etkisiz gorunuyordu. Ayni pencere kuyruktaki
isi (zamanlayici _stop'u temizliyordu), 'connecting' sirasindaki
duraklamayi (isi hataya ceviriyordu) ve iptali (kapali fd'ye yazan
isciler) de bozuyordu.

Testler sabit uyku kullanmaz: kapi (Event) ile olay sirasi kurulur, kalan
beklemeler son tarihli yoklamadir.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.paths import AppEnv
from tests.helpers import DATA, start_server


def wait_until(pred, timeout=20.0):
    """Son tarihli yoklama (sabit uyku yok)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


class PauseRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _job(self, url, **kw) -> DownloadJob:
        env = AppEnv(root=Path(tempfile.mkdtemp(prefix="ferdim-pause-")))
        return DownloadJob(env, env.load_config(), JobSpec(url=url, **kw))

    def _pause_in_start_window(self, job: DownloadJob):
        """DOWNLOADING gecisinde (monitor henuz yayinlanmamisken) bir
        pause thread'i tetikler. Hook JOIN etmez: start() o sirada
        _changed() icinde bekliyor olurdu (kilitlenme)."""
        seen: dict = {}
        armed = {"fire": False}
        done = threading.Event()

        def hook(j):
            if not armed["fire"] or j.state != JobState.DOWNLOADING:
                return
            armed["fire"] = False

            def do_pause():
                j.pause()
                seen["state"] = j.state
                done.set()

            threading.Thread(target=do_pause, daemon=True,
                             name="pause-race").start()

        job.on_change = hook
        return armed, seen, done

    def test_01_pause_in_start_window_returns_paused(self):
        """Duraklatma DOWNLOADING gecisinde gelirse: pause() dondugunde is
        'paused' (eski hata: 'downloading'). Sonra devam edip ayni yaris
        ESKI (olu) monitor varken tekrar denenir."""
        job = self._job(f"{self.base}/slow.bin", segments=4)
        armed, seen, done = self._pause_in_start_window(job)
        for phase in (1, 2):
            armed["fire"] = True
            if phase == 1:
                job.start()
            else:
                job.resume()
            self.assertTrue(done.wait(20), f"faz {phase}: pause donmedi")
            self.assertEqual(seen["state"], JobState.PAUSED,
                             f"faz {phase}: pause 'paused' ile donmedi")
            self.assertEqual(job.state, JobState.PAUSED, f"faz {phase}")
            self.assertEqual(job._inflight, {}, f"faz {phase}: isci kalmadi")
            done.clear()
        job.cancel(delete_files=True)
        self.assertEqual(job.state, JobState.CANCELED)

    def test_02_pause_during_connecting_is_not_an_error(self):
        """probe/hazirlik surerken ('connecting') gelen duraklatma isi
        hataya cevirmemeli (eski hata: 'indirme kaynagi kalmadi')."""
        job = self._job(f"{self.base}/slow.bin", segments=2)
        gate, entered = threading.Event(), threading.Event()
        real_prepare = job._prepare

        def gated_prepare():
            entered.set()
            gate.wait(20)
            real_prepare()

        job._prepare = gated_prepare
        starter = threading.Thread(target=job.start, daemon=True)
        starter.start()
        self.assertTrue(entered.wait(20), "start() hazirliga giremedi")
        self.assertEqual(job.state, JobState.CONNECTING)
        pauser = threading.Thread(target=job.pause, daemon=True)
        pauser.start()
        self.assertTrue(wait_until(lambda: job._stop.is_set()),
                        "pause dur sinyalini kurmadi")
        self.assertTrue(pauser.is_alive(), "pause penceresi kapanmamali")
        gate.set()
        starter.join(20)
        pauser.join(20)
        self.assertFalse(starter.is_alive(), "start() donmedi")
        self.assertFalse(pauser.is_alive(), "pause() donmedi")
        self.assertEqual(job.state, JobState.PAUSED, job.error)
        self.assertNotEqual(job.error, "indirme kaynagi kalmadi")
        job.cancel(delete_files=True)

    def test_03_paused_queued_job_is_not_started_by_ticker(self):
        """Kuyruktaki is duraklatilirsa zamanlayicinin baslatmasi onu
        ezmemeli (eskiden start() _stop'u temizleyip indirmeye basliyordu)."""
        job = self._job(f"{self.base}/file.bin", segments=2)
        self.assertEqual(job.state, JobState.QUEUED)
        job.pause()
        self.assertEqual(job.state, JobState.PAUSED)
        job.start(queued_only=True)          # zamanlayici tick'i
        self.assertEqual(job.state, JobState.PAUSED, "duraklama ezildi")
        self.assertIsNone(job._monitor, "isci havuzu baslamamali")
        # kullanicinin devami hala calisir (queued_only=False)
        job.resume()
        self.assertTrue(wait_until(
            lambda: job.state in (JobState.DONE, JobState.ERROR), 60),
            f"devam etmedi: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.final_path.read_bytes(), DATA)

    def test_04_cancel_during_start_window_wins(self):
        """Baslatma ortasinda iptal: durum 'canceled' kalir, isci havuzu
        hic kurulmaz, kapali fd'ye yazan isci olmaz."""
        job = self._job(f"{self.base}/slow.bin", segments=4)
        gate, entered = threading.Event(), threading.Event()
        real_prepare = job._prepare

        def gated_prepare():
            entered.set()
            gate.wait(20)
            real_prepare()

        job._prepare = gated_prepare
        starter = threading.Thread(target=job.start, daemon=True)
        starter.start()
        self.assertTrue(entered.wait(20), "start() hazirliga giremedi")
        canceler = threading.Thread(target=job.cancel, daemon=True)
        canceler.start()
        self.assertTrue(wait_until(lambda: job._stop.is_set()),
                        "cancel dur sinyalini kurmadi")
        gate.set()
        starter.join(20)
        canceler.join(20)
        self.assertFalse(starter.is_alive(), "start() donmedi")
        self.assertFalse(canceler.is_alive(), "cancel() donmedi")
        self.assertEqual(job.state, JobState.CANCELED)
        self.assertEqual(job._threads, [], "isci havuzu kurulmamali")
        self.assertIsNone(job._fd, "fd kapatilmali")
        self.assertFalse(job.final_path.exists(), "dosya yazilmamali")
        self.assertEqual(job.done, 0)


if __name__ == "__main__":
    unittest.main()
