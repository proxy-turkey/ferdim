"""Birlikte yasam modu (ROADMAP 2.0): hat izleyici ve kuyruk tavani.

/proc/net/dev ayristirmasi sahte metinle, ornekleyici ise enjekte edilen
okuyucu + saat ile calisir: uyku yok. Governor kararlari sahte zamanla
(FakeTime) dogrulanir; varsayilanin kapali oldugu ve kapaliyken hattin
hic okunmadigi ayrica kontrol edilir.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ferdim.api as api
import ferdim.cli as cli
import ferdim.engine.job as jobmod
from ferdim.coexist import (DEFAULT_LATENCY, LatencySignal, LineMonitor,
                            is_line_iface, parse_net_dev)
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.engine.queue import QueueManager
from ferdim.paths import DEFAULTS, AppEnv
from ferdim.profiles import host_key
from tests.helpers import start_server, tmp_root

MB = 1_000_000

NET_DEV = """\
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo:    1000      10    0    0    0     0          0         0     2000      20    0    0    0     0       0          0
  eth0:    5000      50    0    0    0     0          0         0     7000      70    0    0    0     0       0          0
docker0:     100       1    0    0    0     0          0         0      200       2    0    0    0     0       0          0
 wlp2s0:     300       3    0    0    0     0          0         0      400       4    0    0    0     0       0          0
  veth9:     111       1    0    0    0     0          0         0      222       2    0    0    0     0       0          0
"""


class FakeTime:
    def __init__(self, start=1000.0):
        self.t = start

    def monotonic(self):
        return self.t

    def time(self):
        return 1_790_000_000.0

    def sleep(self, _x):
        self.t += 0.01


def make_monitor(case, threshold=0.15):
    """Sahte hat: her ornekte 'box["v"]' sayaci okunur, saat elle ilerler."""
    ft = FakeTime()
    box = {"v": 0}
    mon = LineMonitor(read=lambda: box["v"], clock=ft.monotonic,
                      threshold=threshold)
    case.addCleanup(mon.reset)
    return mon, ft, box


def feed(mon, ft, box, rate, own=0.0, secs=1.0, latency=None):
    """Bir saniyelik trafik ekleyip ornek alir (uyku yok)."""
    ft.t += secs
    box["v"] += int(rate * secs)
    return mon.sample(own, latency=latency)


def make_queue(case):
    """Kuyruk: arka plan is parcaciklari durdurulur (ag yok, yaris yok)."""
    env = AppEnv(root=tmp_root(case, "ferdim-coex-"))
    cfg = env.load_config()
    qm = QueueManager(env, cfg)
    qm._running = False
    for th in (qm._ticker, qm._prober):
        if th is not None:
            th.join(2.0)
    return env, cfg, qm


def fake_job(qm, segments=8, speed=5 * MB):
    """Kuyrukta 'indiriyor' gorunen is: ag baglantisi kurulmaz."""
    job = qm.add(JobSpec(url="http://ornek.test/dosya.bin",
                         segments=segments), paused=True)
    job.total, job.done, job.speed = 100 * MB, MB, speed
    job.state = JobState.DOWNLOADING
    return job


class NetDevTests(unittest.TestCase):
    def test_01_parse_skips_loopback_and_virtual(self):
        # yalniz eth0 (5000+7000) ve wlp2s0 (300+400)
        self.assertEqual(parse_net_dev(NET_DEV), 12_700)

    def test_02_iface_classification(self):
        for name in ("eth0", "enp3s0", "wlp2s0"):
            self.assertTrue(is_line_iface(name), name)
        for name in ("lo", "docker0", "veth9", "virbr0", "tun0", "wg0"):
            self.assertFalse(is_line_iface(name), name)

    def test_03_empty_and_broken_input(self):
        self.assertEqual(parse_net_dev(""), 0)
        self.assertEqual(parse_net_dev("eth0: bozuk satir\n"), 0)


class LatencySignalTests(unittest.TestCase):
    """TTFB sismesi: ana makine basina taban (EMA-min) + guncel EMA."""

    def test_01_ratio_is_one_until_the_baseline_warms(self):
        sig = LatencySignal()
        self.assertEqual(sig.inflation(), 1.0)      # ornek yok
        sig.add("a.test", 0.02)
        self.assertEqual(sig.inflation(), 1.0)      # WARM altinda
        sig.add("a.test", 0.02)
        self.assertEqual(sig.inflation(), 1.0)

    def test_02_slowdown_raises_inflation(self):
        sig = LatencySignal()
        for _ in range(3):
            sig.add("a.test", 0.02)                 # taban 20 ms
        for _ in range(3):
            sig.add("a.test", 0.20)                 # 200 ms'ye cikti
        self.assertGreater(sig.inflation(), DEFAULT_LATENCY)

    def test_03_honest_baseline_is_costly_to_move(self):
        """Taban en iyi degerdir: yavas ornekler tabani yukari tasimaz."""
        sig = LatencySignal()
        for _ in range(3):
            sig.add("a.test", 0.05)
        for _ in range(40):
            sig.add("a.test", 0.05)
        sig.add("a.test", 0.5)
        sig.add("a.test", 0.5)
        self.assertGreater(sig.inflation(), 3.0)

    def test_04_worst_host_wins_and_invalid_values_ignored(self):
        sig = LatencySignal()
        for _ in range(3):
            sig.add("a.test", 0.02)
            sig.add("b.test", 0.10)
        for _ in range(4):
            sig.add("b.test", 0.40)                 # yalniz b yavasladi
        worst = sig.inflation()
        self.assertGreater(worst, DEFAULT_LATENCY)
        before = sig.inflation()
        for bad in ("bozuk", -1.0, 0.0, 10_000.0, None):
            sig.add("b.test", bad)                  # gecersiz: yok sayilir
        self.assertEqual(sig.inflation(), before)

    def test_05_reset_clears_baseline(self):
        sig = LatencySignal()
        for _ in range(3):
            sig.add("a.test", 0.02)
        for _ in range(3):
            sig.add("a.test", 0.4)
        self.assertGreater(sig.inflation(), 1.0)
        sig.reset()
        self.assertEqual(sig.inflation(), 1.0)


class LatencyMonitorTests(unittest.TestCase):
    """Hat sakin olsa da (yabanci trafik yok) gecikme basinci tetikler."""

    def test_01_latency_enters_after_the_enter_window(self):
        mon, ft, box = make_monitor(self)
        mon.sample(0.0)                                # taban ornegi
        # hat sabit (yabanci trafik 0) ama TTFB 3 katina cikti
        states = [feed(mon, ft, box, 10 * MB, own=10 * MB, latency=3.0)
                  for _ in range(3)]
        self.assertEqual(states, [False, False, False], "pencere dolmadi")
        self.assertTrue(feed(mon, ft, box, 10 * MB, own=10 * MB, latency=3.0))
        self.assertTrue(mon.active)
        self.assertEqual(mon.foreign, 0.0, "yabanci trafik yok")
        self.assertGreater(mon.pressure, 0.0, "gecikme basinci raporlanmali")

    def test_02_latency_leaves_after_the_quiet_window(self):
        mon, ft, box = make_monitor(self)
        mon.sample(0.0)
        for _ in range(4):
            feed(mon, ft, box, 10 * MB, own=10 * MB, latency=3.0)
        self.assertTrue(mon.active)
        for _ in range(5):
            feed(mon, ft, box, 10 * MB, own=10 * MB, latency=1.0)
        self.assertTrue(mon.active, "sessizlik penceresi dolmadi")
        for _ in range(6):
            feed(mon, ft, box, 10 * MB, own=10 * MB, latency=1.0)
        self.assertFalse(mon.active)
        self.assertEqual(mon.pressure, 0.0)

    def test_03_below_the_factor_is_not_pressure(self):
        mon, ft, box = make_monitor(self)
        mon.sample(0.0)
        for _ in range(8):
            feed(mon, ft, box, 10 * MB, own=10 * MB, latency=2.0)
        self.assertFalse(mon.active, "esik altinda tavan tutulmaz")
        self.assertGreater(mon.pressure, 0.0)          # yine de raporlanir
        self.assertLess(mon.pressure, 1.0)

    def test_04_factor_is_configurable(self):
        mon, ft, box = make_monitor(self)
        mon.latency_factor = 1.5
        mon.sample(0.0)
        for _ in range(4):
            feed(mon, ft, box, 10 * MB, own=10 * MB, latency=2.0)
        self.assertTrue(mon.active, "daha siki esik daha erken tetikler")

    def test_05_reset_clears_latency(self):
        mon, ft, box = make_monitor(self)
        mon.sample(0.0)
        for _ in range(4):
            feed(mon, ft, box, 10 * MB, own=10 * MB, latency=3.0)
        self.assertTrue(mon.active)
        mon.reset()
        self.assertEqual(mon.latency, 1.0)
        self.assertFalse(mon.active)


class HysteresisTests(unittest.TestCase):
    def test_01_enters_only_after_enter_window(self):
        mon, ft, box = make_monitor(self)
        self.assertFalse(mon.sample(0.0))            # taban ornegi
        # yabanci trafik = 5MB/sn, kapasite 10MB/sn -> oran 0.5 > 0.15
        states = [feed(mon, ft, box, 10 * MB, own=5 * MB) for _ in range(3)]
        self.assertEqual(states, [False, False, False], "3 sn dolmadan aktif olmaz")
        self.assertTrue(feed(mon, ft, box, 10 * MB, own=5 * MB))
        self.assertTrue(mon.active)
        self.assertAlmostEqual(mon.pressure, 0.5, places=2)

    def test_02_leaves_only_after_quiet_window(self):
        mon, ft, box = make_monitor(self)
        mon.sample(0.0)
        for _ in range(4):
            feed(mon, ft, box, 10 * MB, own=5 * MB)
        self.assertTrue(mon.active)
        # trafik susar: 10 sn dolmadan tavan birakilmaz
        for _ in range(5):
            self.assertTrue(feed(mon, ft, box, 5 * MB, own=5 * MB))
        for _ in range(6):
            feed(mon, ft, box, 5 * MB, own=5 * MB)
        self.assertFalse(mon.active)
        self.assertEqual(mon.pressure, 0.0)

    def test_03_own_traffic_is_subtracted(self):
        """FerDim kendi hattini doyururken mod tetiklenmemeli."""
        mon, ft, box = make_monitor(self)
        mon.sample(0.0)
        for _ in range(8):
            feed(mon, ft, box, 10 * MB, own=9.9 * MB)
        self.assertFalse(mon.active)
        self.assertLess(mon.pressure, 0.15)
        # ayni hat hizi, kendi trafigimiz dusunce yabanci trafik belirir
        for _ in range(4):
            feed(mon, ft, box, 10 * MB, own=5 * MB)
        self.assertTrue(mon.active)

    def test_04_reset_clears_state(self):
        mon, ft, box = make_monitor(self)
        mon.sample(0.0)
        for _ in range(4):
            feed(mon, ft, box, 10 * MB, own=5 * MB)
        self.assertTrue(mon.active)
        mon.reset()
        self.assertFalse(mon.active)
        self.assertEqual((mon.pressure, mon.capacity, mon.foreign), (0.0, 0.0, 0.0))


class QueueCapTests(unittest.TestCase):
    def test_01_default_off_never_reads_the_line(self):
        env, cfg, qm = make_queue(self)
        calls = []
        qm.coexist = LineMonitor(read=lambda: calls.append(1) or 0)
        job = fake_job(qm, segments=8)
        self.assertFalse(DEFAULTS["coexist"], "varsayilan kapali olmali")
        self.assertFalse(cfg["coexist"])
        for _ in range(3):
            qm._tick()
        self.assertEqual(calls, [], "kapaliyken /proc/net/dev okunmaz")
        self.assertIsNone(job._ext_cap)
        self.assertEqual(job._allowed, 8)

    def test_02_pressure_caps_and_release_restores(self):
        env, cfg, qm = make_queue(self)
        mon, ft, box = make_monitor(self)
        qm.coexist = mon
        job = fake_job(qm, segments=8, speed=5 * MB)
        cfg["coexist"] = True
        mon.sample(0.0)                     # taban: hat izleyici ilk ornek
        for _ in range(4):
            ft.t += 1.0
            box["v"] += 10 * MB
            qm._coexist_tick()
        self.assertTrue(qm.coexist_status()["active"])
        self.assertEqual(job._ext_cap, 4, "yarisi: 8 -> 4")
        self.assertEqual(job._allowed, 4)
        self.assertEqual(job.extra.get("adaptive_reason"), "coexist")
        # trafik susar: 10 sn sessizlikten sonra tavan birakilir
        for _ in range(6):
            ft.t += 1.0
            box["v"] += 5 * MB
            qm._coexist_tick()
        self.assertEqual(job._ext_cap, 4, "sessizlik penceresi dolmadi")
        for _ in range(6):
            ft.t += 1.0
            box["v"] += 5 * MB
            qm._coexist_tick()
        self.assertIsNone(job._ext_cap, "tavan birakilmali")
        self.assertNotEqual(job.extra.get("adaptive_reason"), "coexist")
        self.assertEqual(job._allowed, 4, "yumusak donus: sicrama yok")

    def test_03_disabling_releases_cap_at_once(self):
        env, cfg, qm = make_queue(self)
        mon, ft, box = make_monitor(self)
        qm.coexist = mon
        job = fake_job(qm, segments=6, speed=2 * MB)
        cfg["coexist"] = True
        mon.sample(0.0)
        for _ in range(4):
            ft.t += 1.0
            box["v"] += 10 * MB
            qm._coexist_tick()
        self.assertEqual(job._ext_cap, 3)
        cfg["coexist"] = False
        qm._tick()
        self.assertIsNone(job._ext_cap)
        self.assertFalse(qm.coexist_status()["active"])

    def test_04_single_link_job_stays_alive(self):
        env, cfg, qm = make_queue(self)
        mon, ft, box = make_monitor(self)
        qm.coexist = mon
        job = fake_job(qm, segments=1, speed=MB)
        cfg["coexist"] = True
        mon.sample(0.0)
        for _ in range(4):
            ft.t += 1.0
            box["v"] += 10 * MB
            qm._coexist_tick()
        self.assertEqual(job._ext_cap, 1)
        self.assertEqual(job._allowed, 1, "en az bir baglanti kalir")

    def test_05_paused_job_gets_cap_too(self):
        """Tavan tum kosabilecek islere uygulanir; duraklatilan is de dahil."""
        env, cfg, qm = make_queue(self)
        mon, ft, box = make_monitor(self)
        qm.coexist = mon
        job = fake_job(qm, segments=8, speed=5 * MB)
        job.state = JobState.PAUSED          # basinc baslamadan duraklatildi
        cfg["coexist"] = True
        mon.sample(0.0)
        for _ in range(4):
            ft.t += 1.0
            box["v"] += 10 * MB
            qm._coexist_tick()
        self.assertEqual(job._ext_cap, 4, "duraklatilmis is de tavanlanmali")

    def test_06_pause_under_pressure_off_then_resume(self):
        """Regresyon: basinc altinda duraklat, modu kapat, sonra devam et.

        Eski hata: kapanma gecisi duraklatilmis isi atliyordu; is devam
        edince dis tavan sonsuza kadar uzerinde kaliyordu.
        """
        env, cfg, qm = make_queue(self)
        mon, ft, box = make_monitor(self)
        qm.coexist = mon
        job = fake_job(qm, segments=8, speed=5 * MB)
        cfg["coexist"] = True
        mon.sample(0.0)
        for _ in range(4):
            ft.t += 1.0
            box["v"] += 10 * MB
            qm._coexist_tick()
        self.assertEqual(job._ext_cap, 4)
        job.state = JobState.PAUSED          # kullanici duraklatti
        cfg["coexist"] = False               # ... ve modu kapatti
        qm._coexist_tick()
        self.assertIsNone(job._ext_cap,
                          "mod kapaninca duraklatilmis isin tavani da kalkmali")
        job.state = JobState.DOWNLOADING     # devam
        for _ in range(4):
            qm._tick()
        self.assertIsNone(job._ext_cap, "devam eden is tavansiz kosmali")
        self.assertNotEqual(job.extra.get("adaptive_reason"), "coexist")
        self.assertFalse(qm.coexist_status()["enabled"])

    def test_07_pressure_ends_while_paused_releases(self):
        env, cfg, qm = make_queue(self)
        mon, ft, box = make_monitor(self)
        qm.coexist = mon
        job = fake_job(qm, segments=8, speed=5 * MB)
        cfg["coexist"] = True
        mon.sample(0.0)
        for _ in range(4):
            ft.t += 1.0
            box["v"] += 10 * MB
            qm._coexist_tick()
        self.assertEqual(job._ext_cap, 4)
        job.state = JobState.PAUSED
        for _ in range(12):                  # hat susar (mod acik kalir)
            ft.t += 1.0
            qm._coexist_tick()
        self.assertIsNone(job._ext_cap, "duraklatilmis isin tavani birakilmali")

    def test_08_finished_job_never_keeps_cap(self):
        env, cfg, qm = make_queue(self)
        mon, ft, box = make_monitor(self)
        qm.coexist = mon
        job = fake_job(qm, segments=8, speed=5 * MB)
        cfg["coexist"] = True
        mon.sample(0.0)
        for _ in range(4):
            ft.t += 1.0
            box["v"] += 10 * MB
            qm._coexist_tick()
        self.assertEqual(job._ext_cap, 4)
        job.state = JobState.DONE
        for _ in range(12):                  # hat susar
            ft.t += 1.0
            qm._coexist_tick()
        self.assertIsNone(job._ext_cap, "biten is tavan tasimamali")
        # biten is yeniden kuyruga alinip mod kapaliyken kosarsa da tavansiz
        job.state = JobState.QUEUED
        cfg["coexist"] = False
        qm._tick()
        self.assertIsNone(job._ext_cap)

    def test_09_latency_alone_caps_the_pool(self):
        """Yabanci trafik yokken TTFB sismesi de tavani kurar."""
        env, cfg, qm = make_queue(self)
        mon, ft, box = make_monitor(self)
        qm.coexist = mon
        job = fake_job(qm, segments=8, speed=5 * MB)
        cfg["coexist"] = True
        # isin kendi baglantilari: once hizli, sonra 10 kat yavas
        for _ in range(3):
            job._lat.add("ornek.test", 0.02)
        for _ in range(4):
            job._lat.add("ornek.test", 0.20)
        self.assertGreater(job.latency_inflation(), DEFAULT_LATENCY)
        mon.sample(0.0)                      # taban ornegi: hat sakin
        for _ in range(4):
            ft.t += 1.0                  # trafik sabit: foreign = 0
            qm._coexist_tick()
        self.assertEqual(qm.coexist.foreign, 0.0)
        self.assertTrue(qm.coexist_status()["active"], "gecikme basinci")
        self.assertEqual(qm.coexist_status()["latency"],
                         round(job.latency_inflation(), 3))
        self.assertEqual(job._ext_cap, 4, "tavan gecikme ile de kurulur")
        # gecikme duzelir: sessizlik penceresinden sonra tavan birakilir
        for _ in range(6):
            job._lat.add("ornek.test", 0.02)
        self.assertLess(job.latency_inflation(), DEFAULT_LATENCY)
        for _ in range(12):
            ft.t += 1.0
            qm._coexist_tick()
        self.assertIsNone(job._ext_cap)

    def test_10_latency_factor_from_config(self):
        env, cfg, qm = make_queue(self)
        mon, ft, box = make_monitor(self)
        qm.coexist = mon
        job = fake_job(qm, segments=8, speed=5 * MB)
        cfg["coexist"] = True
        cfg["coexist_latency"] = 1.5         # daha siki esik
        for _ in range(3):
            job._lat.add("ornek.test", 0.02)
        for _ in range(3):
            job._lat.add("ornek.test", 0.04)  # 2x: 1.5 esigini asar
        mon.sample(0.0)
        for _ in range(4):
            ft.t += 1.0
            qm._coexist_tick()
        self.assertEqual(qm.coexist.latency_factor, 1.5)
        self.assertTrue(qm.coexist_status()["active"])
        self.assertIsNotNone(job._ext_cap)

    def test_11_latency_ignored_when_mode_off(self):
        env, cfg, qm = make_queue(self)
        mon, ft, box = make_monitor(self)
        qm.coexist = mon
        job = fake_job(qm, segments=8, speed=5 * MB)
        for _ in range(3):
            job._lat.add("ornek.test", 0.02)
        for _ in range(4):
            job._lat.add("ornek.test", 0.30)
        mon.sample(0.0)
        for _ in range(4):
            ft.t += 1.0
            qm._coexist_tick()
        self.assertFalse(qm.coexist_status()["active"])
        self.assertIsNone(job._ext_cap)


class ReadFailureTests(unittest.TestCase):
    """/proc/net/dev okunamazsa tavan tutulmaz (fail-open).

    Eski hata: _sample_locked hata durumunda 'active' degerini donduruyordu;
    hat bilgisi kaybolunca is kalici olarak yarim baglantiyla kaliyordu.
    """

    def _monitor(self):
        ft = FakeTime()
        box = {"v": 0, "ok": True}
        mon = LineMonitor(
            read=lambda: box["v"] if box["ok"] else -1,
            clock=ft.monotonic)
        self.addCleanup(mon.reset)
        return mon, ft, box

    def test_01_read_failure_releases_pressure(self):
        mon, ft, box = self._monitor()
        mon.sample(0.0)
        for _ in range(4):
            ft.t += 1.0
            box["v"] += 10 * MB
            mon.sample(5 * MB)
        self.assertTrue(mon.active)
        box["ok"] = False                    # sayac okunamaz oldu
        ft.t += 1.0
        self.assertFalse(mon.sample(5 * MB), "okunamayan sayac = azami guc")
        self.assertEqual(mon.pressure, 0.0)
        # sayac geri gelince taban yeniden kurulur, eski basinc donmez
        box["ok"] = True
        ft.t += 1.0
        self.assertFalse(mon.sample(5 * MB))
        for _ in range(4):                   # yeni basinc yine algilanir
            ft.t += 1.0
            box["v"] += 10 * MB
            mon.sample(5 * MB)
        self.assertTrue(mon.active, "sayac duzelince izleme yeniden calisir")

    def test_02_queue_releases_cap_when_read_fails(self):
        env, cfg, qm = make_queue(self)
        ft = FakeTime()
        box = {"v": 0, "ok": True}
        qm.coexist = LineMonitor(
            read=lambda: box["v"] if box["ok"] else -1,
            clock=ft.monotonic)
        job = fake_job(qm, segments=8, speed=5 * MB)
        cfg["coexist"] = True
        qm.coexist.sample(0.0)
        for _ in range(4):
            ft.t += 1.0
            box["v"] += 10 * MB
            qm._coexist_tick()
        self.assertEqual(job._ext_cap, 4)
        box["ok"] = False
        ft.t += 1.0
        qm._coexist_tick()
        self.assertIsNone(job._ext_cap,
                          "okunamayan /proc/net/dev tavani birakmali")
        self.assertFalse(qm.coexist_status()["active"])


class GovernedCapTests(unittest.TestCase):
    """Governor dis tavani asla yoklamaz; tavan kalkinca yeniden tirmanir."""

    def setUp(self):
        self.ft = FakeTime()
        self.real_time = jobmod.time
        jobmod.time = self.ft

    def tearDown(self):
        jobmod.time = self.real_time

    def _job(self, segments=8, allowed=8):
        env = AppEnv(root=tmp_root(self, "ferdim-coex-gov-"))
        cfg = env.load_config()
        job = DownloadJob(env, cfg, JobSpec(url="http://ornek.test/f.bin",
                                            segments=segments))
        job.kind = "http"
        job.state = JobState.DOWNLOADING
        job.total = 100_000_000
        job._pending = [(0, 99_999_999)]
        job._allowed = allowed
        job._seg_max = segments
        job._gov["t"] = self.ft.t
        job._gov["done"] = job.done
        return job

    def _tick(self, job, bytes_per_sec, dt=2.6):
        self.ft.t += dt
        job.done += int(bytes_per_sec * dt)
        job._governor_tick()

    def test_01_never_probes_above_cap(self):
        job = self._job(segments=8, allowed=8)
        job.set_external_cap(2)
        self.assertEqual(job._allowed, 2)
        for _ in range(12):
            self._tick(job, 10 * MB)
        self.assertEqual(job._allowed, 2, "tavan asilmamali")
        self.assertFalse(job._gov["probe"], "tavan ustunde kesif yok")

    def test_02_release_resumes_up_probe(self):
        job = self._job(segments=8, allowed=8)
        job.set_external_cap(2)
        for _ in range(3):
            self._tick(job, 10 * MB)
        self.assertEqual(job._allowed, 2)
        job.set_external_cap(None)
        self._tick(job, 10 * MB)
        self.assertEqual(job._allowed, 3, "tavan kalkinca yukari kesif")
        for _ in range(30):
            self._tick(job, 10 * MB)
        self.assertGreater(job._allowed, 3, "tirmanma devam etmeli")
        self.assertLessEqual(job._allowed, 8)

    def test_03_cap_shrink_keeps_adaptive_marker(self):
        job = self._job(segments=8, allowed=8)
        job.set_external_cap(4)
        self.assertEqual(job.extra.get("adaptive"), 4)
        self.assertEqual(job.extra.get("adaptive_reason"), "coexist")
        job.set_external_cap(None)
        self.assertNotIn("adaptive_reason", job.extra)
        self.assertLessEqual(job._gov["next_up"], self.ft.t)


class JobLatencyTests(unittest.TestCase):
    """Aralik isi kendi TTFB'sini olcer: ek istek acilmaz."""

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_range_requests_feed_the_latency_signal(self):
        env = AppEnv(root=tmp_root(self, "ferdim-lat-"))
        cfg = env.load_config()
        job = DownloadJob(env, cfg,
                          JobSpec(url=f"{self.base}/tiny.bin", segments=2))
        job.start()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 30 and job.state not in (
                JobState.DONE, JobState.ERROR):
            time.sleep(0.05)
        self.assertEqual(job.state, JobState.DONE, job.error)
        host = host_key(self.base)
        stats = job._lat._hosts.get(host)
        self.assertIsNotNone(stats, "TTFB ornegi toplanmadi")
        self.assertGreaterEqual(stats["n"], 2, "probe + aralik istegi")
        self.assertGreater(stats["base"], 0.0)
        self.assertGreaterEqual(job.latency_inflation(), 1.0)


class SurfaceTests(unittest.TestCase):
    """API ve CLI yuzeyi: stats anahtari, on/off komutu, config temizligi."""

    def setUp(self):
        self.env, self.cfg, self.qm = make_queue(self)
        self.ctx = {"qm": self.qm, "env": self.env, "cfg": self.cfg,
                    "set_cfg": self.env.save_config}

    def test_01_stats_reports_coexist(self):
        st = api.handle(self.ctx, "stats", {})
        self.assertEqual(st["coexist"]["enabled"], False)
        self.assertEqual(st["coexist"]["active"], False)
        self.assertEqual(st["coexist"]["pressure"], 0.0)

    def test_02_command_toggles_and_persists(self):
        r = api.handle(self.ctx, "coexist", {"on": True})
        self.assertTrue(r["ok"])
        self.assertTrue(r["enabled"])
        self.assertTrue(self.cfg["coexist"])
        self.assertEqual(self.env.load_config()["coexist"], True)
        r = api.handle(self.ctx, "coexist", {"on": False})
        self.assertFalse(r["enabled"])
        self.assertFalse(self.env.load_config()["coexist"])

    def test_03_config_sanitises_threshold(self):
        cfg = dict(DEFAULTS)
        cfg["coexist"] = "yes"
        cfg["coexist_threshold"] = "0.5"
        api._sanitize_config(cfg)
        self.assertIs(cfg["coexist"], True)
        self.assertEqual(cfg["coexist_threshold"], 0.5)
        cfg["coexist_threshold"] = "bozuk"
        cfg["coexist"] = ""
        api._sanitize_config(cfg)
        self.assertIs(cfg["coexist"], False)
        self.assertEqual(cfg["coexist_threshold"], 0.15)

    def test_03b_config_sanitises_latency_factor(self):
        cfg = dict(DEFAULTS)
        self.assertEqual(cfg["coexist_latency"], 2.5)
        cfg["coexist_latency"] = "3.5"
        api._sanitize_config(cfg)
        self.assertEqual(cfg["coexist_latency"], 3.5)
        for bad, want in (("1.0", 1.2), ("99", 20.0), ("bozuk", 2.5)):
            cfg["coexist_latency"] = bad
            api._sanitize_config(cfg)
            self.assertEqual(cfg["coexist_latency"], want, bad)

    def test_04_cli_parses_actions(self):
        p = cli.build_parser()
        self.assertEqual(p.parse_args(["coexist"]).action, "status")
        self.assertEqual(p.parse_args(["coexist", "on"]).action, "on")
        self.assertEqual(p.parse_args(["coexist", "off"]).action, "off")

    def test_05_latency_factor_is_settable(self):
        """coexist_latency config-set ile yazilir ve guvenli araliga cekilir."""
        r = api.handle(self.ctx, "config-set",
                       {"config": {"coexist_latency": "3.5"}})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.cfg["coexist_latency"], 3.5)
        self.assertEqual(self.env.load_config()["coexist_latency"], 3.5)
        api.handle(self.ctx, "config-set",
                   {"config": {"coexist_latency": "bozuk"}})
        self.assertEqual(self.cfg["coexist_latency"], 2.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
