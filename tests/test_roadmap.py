"""Roadmap 1.4.x testleri: ayna sagligi (agirlik, ceza, failover, stall),
saglama (SHA-1/SHA-512, yan dosya), kuyruk kalıcılığı ve kategori
pencereleri, izlenen klasörler, web erişilebilirligi."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import api, util
from ferdim.engine import httpcli
from ferdim.engine import job as job_mod
from ferdim.engine.job import (SWRR_FLOOR, DownloadJob, JobSpec, JobState, _Src,
                               backoff_delay)
from ferdim.engine.queue import QueueManager
from ferdim.i18n import S
from ferdim import paths
from ferdim.paths import AppEnv
from ferdim.watch import FolderWatcher, parse_url_list
from tests.helpers import DATA, DATA_SHA, TINY, start_server


def wait_state(job, states, timeout=60):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if job.state in states:
            return True
        time.sleep(0.1)
    return False


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dead_job(url, cfg, **kw):
    """Ag istemeden _process_range deneysin diye hazirlanmis is."""
    env = AppEnv(root=Path(tempfile.mkdtemp(prefix="ferdim-unit-")))
    job = DownloadJob(env, cfg, JobSpec(url=url, **kw))
    job.state = JobState.DOWNLOADING
    job.total = 1_000_000
    env.download_dir.mkdir(parents=True, exist_ok=True)
    job._fd = os.open(env.download_dir / "unit.part",
                      os.O_RDWR | os.O_CREAT, 0o644)
    return job


def closed_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class MirrorHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()
        cls.env = AppEnv(root=Path(tempfile.mkdtemp(prefix="ferdim-rm-")))
        cls.cfg = cls.env.load_config()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _job(self, url, cfg=None, **kw):
        return DownloadJob(self.env, cfg or self.cfg, JobSpec(url=url, **kw))

    def _done(self, job, timeout=60):
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR), timeout),
                        f"suren durum: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path), DATA_SHA)

    def test_01_dead_mirror_does_not_kill_job(self):
        """Birincil saglamken 404 veren ayna isi bitirmemeli."""
        job = self._job(f"{self.base}/file.bin", segments=8,
                        mirrors=[f"{self.base}/yok-ayna.bin"])
        job.start()
        self._done(job)
        snap = job.snapshot()
        self.assertEqual(snap["sources_dead"], 1)
        self.assertEqual(snap["mirrors"], 1)

    def test_02_size_mismatch_mirror_rejected(self):
        """Baska boyutta dosya veren ayna Content-Range'den yakalanir."""
        job = self._job(f"{self.base}/file.bin", segments=8,
                        mirrors=[f"{self.base}/tiny.bin"])
        job.start()
        self._done(job)
        self.assertEqual(job.snapshot()["sources_dead"], 1)

    def test_03_rangeless_mirror_no_restart_loop(self):
        """Kesifte ulasilamayan, sonra Range'siz yanit veren ayna isi
        bastan baslatmamali (sonsuza kadar restart dongusu)."""
        job = self._job(f"{self.base}/file.bin", segments=8,
                        mirrors=[f"{self.base}/lazyno.bin"])
        job.start()
        self._done(job)
        self.assertEqual(job._restarts, 0, "ayna yuzunden restart yapilmamali")
        self.assertEqual(job.snapshot()["sources_dead"], 1)

    def test_04_primary_down_mirror_serves(self):
        """Birincil kaynak kapali: is aynadan baslamali ve bitmeli."""
        job = self._job(f"http://127.0.0.1:{closed_port()}/kapali.bin",
                        segments=6, mirrors=[f"{self.base}/file.bin"])
        job.start()
        self._done(job)

    def test_05_stalled_mirror_fails_over(self):
        """Akisi duran (stall) ayna birakilip baska kaynaktan devam."""
        cfg = dict(self.cfg)
        cfg["stall_timeout"] = 5
        job = self._job(f"{self.base}/file.bin", cfg, segments=6,
                        mirrors=[f"{self.base}/trickle.bin"])
        t0 = time.monotonic()
        job.start()
        self._done(job, 90)
        self.assertLess(time.monotonic() - t0, 60)
        mirror = next(s for s in job._srcs if not s.primary)
        self.assertGreaterEqual(mirror.fails, 1)
        self.assertTrue(mirror.streak >= 1 or mirror.dead)

    def test_06_swrr_weights_follow_measured_speed(self):
        """Olculen hiza gore pay: 1:2:1 hizli kaynak iki kat baglanti alir;
        henuz olculmemis kaynaklar esit paylasir."""
        job = self._job(f"{self.base}/file.bin", segments=8)
        a, b, c = (_Src("http://a/f", True, 8), _Src("http://b/f", False, 8),
                   _Src("http://c/f", False, 8))
        job._srcs = [a, b, c]
        now = time.monotonic()
        picks = [job._pick_locked(now)[0] for _ in range(600)]
        self.assertEqual(picks.count(b), 200)      # esit agirlik: 600/3
        self.assertEqual(picks.count(a), 200)
        a.ema, b.ema, c.ema = 1_000_000.0, 2_000_000.0, 1_000_000.0
        picks = [job._pick_locked(now)[0] for _ in range(800)]
        self.assertAlmostEqual(picks.count(b) / 800, 0.5, delta=0.02)
        self.assertAlmostEqual(picks.count(a) / 800, 0.25, delta=0.02)
        # cok yavas kaynak tamamen elenmez: en iyinin %5'i kadar pay alir
        c.ema = 1.0
        picks = [job._pick_locked(now)[0] for _ in range(2000)]
        self.assertGreater(picks.count(c), 0)
        self.assertLess(picks.count(c), 2000 * (SWRR_FLOOR + 0.02))

    def test_07_swrr_skips_cooled_and_dead(self):
        job = self._job(f"{self.base}/file.bin")
        a, b = _Src("http://a/f", True, 8), _Src("http://b/f", False, 8)
        job._srcs = [a, b]
        now = time.monotonic()
        b.cooldown = now + 1000
        self.assertEqual([job._pick_locked(now)[0] for _ in range(5)],
                         [a] * 5, "cezali kaynak atlanmali")
        b.dead = True
        self.assertEqual(job._pick_locked(now)[0], a)
        a.cooldown = now + 1000
        self.assertEqual(job._pick_locked(now), (None, True), "hepsi cezali: bekle")
        a.dead = True
        self.assertEqual(job._pick_locked(now), (None, False), "canli kaynak yok")

    def test_08_backoff_is_exponential_with_jitter(self):
        for seed in (1, 2, 3):
            rng = random.Random(seed)
            for attempt in range(1, 9):
                base = min(float(2 ** attempt), 15.0)
                d = backoff_delay(attempt, rng=rng)
                self.assertGreaterEqual(d, base * 0.5)
                self.assertLessEqual(d, base * 1.5)
        vals = {round(backoff_delay(3, rng=random.Random(s)), 4) for s in range(20)}
        self.assertGreater(len(vals), 5, "jitter yok: tum denemeler esit bekliyor")

    def test_09_source_dies_after_streak_unless_progress(self):
        job = self._job(f"{self.base}/file.bin")
        job._retries = 2
        s = job._srcs[0]
        job._src_fail(s, "conn", cooldown=False)
        job._src_fail(s, "conn", cooldown=False)
        self.assertFalse(s.dead)
        job._src_ok(s)                      # ilerleme gordu: seri sifirlandi
        job._src_fail(s, "conn", cooldown=False)
        self.assertFalse(s.dead, "ilerleyen kaynak olmemeli")
        for _ in range(3):
            job._src_fail(s, "conn", cooldown=False)
        self.assertTrue(s.dead)
        other = _Src("http://b/f", False, 8)
        job._srcs.append(other)
        job._src_fail(other, "gone")
        self.assertTrue(other.dead, "gecersiz kaynak aninda olu sayilir")
        self.assertEqual(job.snapshot()["sources_dead"], 2)
        self.assertEqual(job._pick_locked(time.monotonic()), (None, False))

    def test_10_demote_halves_cap_and_recovers(self):
        job = self._job(f"{self.base}/file.bin", segments=32)
        job._allowed = 32
        b = _Src("http://b/f", False, 32)
        job._srcs = [job._srcs[0], b]
        now = time.monotonic()
        for want in (16, 8, 4):
            job._demote(b, "HTTP 429", 3.0, count=False)
            self.assertEqual(b.cap, want)
        self.assertFalse(job._allowed < 32, "diger kaynak varken is kuculmemeli")
        self.assertFalse(b.dead)
        # ceza penceresi gecti: kapasite yavasca toparlar
        job._last_limit_ts = time.monotonic() - 61
        job._pick_locked(time.monotonic())
        self.assertEqual(b.cap, 8)
        # ardisik limit sinyalleri kaynak oldurebilir
        job._retries = 1
        job._demote(b, "HTTP 429", 3.0)
        job._demote(b, "HTTP 429", 3.0)
        self.assertTrue(b.dead)

    def test_11_read_failure_requeues_remaining_range(self):
        """Okuma hatasinda kalan aralik kuyruga donmeli (aksinde devam
        edilince 'eksik veri')."""
        cfg = dict(self.cfg)
        cfg["retries"] = 0

        class Boom:
            headers = {"Content-Range": "bytes 0-999999/1000000"}

            def read(self, _n):
                raise OSError("disk yok")

            def close(self):
                pass

        job = dead_job(f"{self.base}/file.bin", cfg)
        real = httpcli.open_range
        httpcli.open_range = lambda *a, **k: Boom()
        try:
            cooldown, exit_worker = job._process_range((0, 999_999),
                                                       job.spec.options(cfg))
        finally:
            httpcli.open_range = real
        self.assertTrue(exit_worker)
        self.assertEqual(job.state, JobState.ERROR)
        self.assertIn((0, 999_999), job._pending, "kalan aralik kuyrukta olmali")
        job.cancel(delete_files=True)

    # --- duraklama / devam: elde kalan aralik kaybolmaz ---
    class _BlockedResp:
        """read() kapi acilana kadar bekleyen sahte yanit (yavas okuma).
        Content-Range isin bildirdigi boyutla tutarli olmali: motor her
        yanitin baslangic/boyutunu dogrular."""

        status = 206

        def __init__(self, gate, job, first):
            self._gate = gate
            self.headers = {"Content-Range": f"bytes {first}-{job.total - 1}/{job.total}"}

        def read(self, _n):
            self._gate.wait(10)
            return b"x" * 16

        def close(self):
            pass

    def _lingering_worker(self, job, first=0):
        """Sahte (kapali) yanitla bir isci baslatir. Yanit, test bitene
        kadar yerinde kalir; isci gercekten okumada bekliene kadar bekler."""
        gate = threading.Event()
        real = httpcli.open_range
        httpcli.open_range = lambda *a, **k: self._BlockedResp(gate, job, first)
        self.addCleanup(lambda: (gate.set(), setattr(httpcli, "open_range", real)))

        def run():
            job._process_range((first, job.total - 1),
                               job.spec.options(job.cfg), 0, job._gen)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t0 = time.monotonic()
        while not job._inflight and time.monotonic() - t0 < 5:
            time.sleep(0.01)
        time.sleep(0.3)                  # _open + ilk read'a gecmesi icin
        self.assertTrue(t.is_alive(), "isci okumada beklemeli")
        self.assertEqual(job._inflight, {0: (first, job.total - 1)})
        return t, gate

    def test_13_inflight_range_is_checkpointed(self):
        """Denetim noktasi kuyruktaki VE iscinin elindeki araliklari yazar:
        yavas bir baglantida duraklama (ya da cokme) sonrasi o aralik
        kaybolup 'eksik veri' uretmez."""
        cfg = dict(self.cfg)
        job = dead_job(f"{self.base}/file.bin", cfg, segments=2)
        job._pending = [(500_000, 700_000)]
        job._inflight[0] = (0, 299_999)
        job._checkpoint(force=True)
        ranges = util.read_json(job.meta_path)["ranges"]
        self.assertEqual(sorted(map(tuple, ranges)), [(0, 299_999), (500_000, 700_000)])
        job.cancel(delete_files=True)

    def test_14_pause_with_lingering_worker_then_resume(self):
        """Okumada takili isci varken duraklama: komut dondugunde aralik
        kayittadir ve durum 'paused'; devam edilince yeni kusak araligi
        indirir, eski isci (okumasi bitince) sessizce cekilir - araligi
        ikinci kez kuyruga eklemez, veri yazmaz."""
        cfg = dict(self.cfg)
        job = dead_job(f"{self.base}/file.bin", cfg, segments=2)
        t, gate = self._lingering_worker(job)
        job._threads = []              # birlesme beklemesin: isci hala okumada
        job.pause()
        job._fold_threads()
        self.assertEqual(job.state, JobState.PAUSED)
        meta = util.read_json(job.meta_path)
        self.assertEqual([tuple(r) for r in meta["ranges"]], [(0, 999_999)])
        # devam: _prepare gibi kuyruk kayittan yuklenir, kusak degisir
        job._pending = [tuple(r) for r in meta["ranges"]]
        job._stop.clear()
        job._gen += 1
        gate.set()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(job._pending, [(0, 999_999)])   # kopya yok
        self.assertEqual(job._inflight, {})
        self.assertEqual(job.done, 0)                          # veri yazmadi
        job.cancel(delete_files=True)

    def test_15_pause_lingering_worker_requeues_when_still_stopped(self):
        """Duraklama surerken okumasi biten isci kalan araligi iade eder."""
        cfg = dict(self.cfg)
        job = dead_job(f"{self.base}/file.bin", cfg, segments=2)
        t, gate = self._lingering_worker(job)
        job._stop.set()
        gate.set()
        t.join(5)
        self.assertEqual(job._pending, [(0, 999_999)])
        self.assertEqual(job._inflight, {})
        self.assertEqual(job.done, 0)
        job.cancel(delete_files=True)

    def test_16_pause_command_returns_when_paused(self):
        """pause() havuz kapanana kadar bekler: komut dondugunde durum
        'paused' dir ve araliklar kaydedilmistir (CLI/GTK aninda gorur)."""
        # hiz limiti indirmeyi yavaslatir: pause gercekten ORTADA gelir
        # (limitsiz 4 MB yerel indirme duraklatmadan once bitebilirdi)
        job = self._job(f"{self.base}/slow.bin", segments=4, limit=512_000)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DOWNLOADING,)))
        t0 = time.monotonic()
        while job.done < 256 * 1024 and time.monotonic() - t0 < 20:
            time.sleep(0.01)
        self.assertGreaterEqual(job.done, 256 * 1024, "indirme ilerlemedi")
        job.pause()
        self.assertEqual(job.state, JobState.PAUSED, job.state)
        meta = util.read_json(job.meta_path)
        covered = sum(e - s + 1 for s, e in meta["ranges"])
        self.assertEqual(covered + job.done, len(DATA), "araliklar + indirilen = toplam")
        self.assertEqual(job._inflight, {})
        job._bucket.set_rate(0)      # devam hizli bitsin (limit testi degil)
        job.resume()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path), DATA_SHA)

    def test_17_pause_and_cancel_are_prompt_under_speed_limit(self):
        """Hiz limitli iste 32 isci tek kovada sirayla bekler; durdurma
        yine de hizli olmali (eski davranis: pause 8 sn sonra bile
        'downloading', cancel dakikalar suruyordu)."""
        job = self._job(f"{self.base}/file.bin", segments=16, limit=60_000)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DOWNLOADING,)))
        time.sleep(1.2)                      # isciler kovaya takilsin
        t0 = time.monotonic()
        job.pause()
        dt = time.monotonic() - t0
        self.assertEqual(job.state, JobState.PAUSED, job.state)
        self.assertLess(dt, 2.5, f"pause {dt:.1f} sn surdu")
        self.assertEqual(job._inflight, {})
        job.resume()
        self.assertTrue(wait_state(job, (JobState.DOWNLOADING,)))
        time.sleep(1.0)
        t0 = time.monotonic()
        job.cancel()
        dt = time.monotonic() - t0
        self.assertEqual(job.state, JobState.CANCELED, job.state)
        self.assertLess(dt, 3.5, f"cancel {dt:.1f} sn surdu")
        time.sleep(0.5)
        self.assertFalse([t for t in job._threads if t.is_alive()])

    def test_18_token_bucket_wait_is_abortable(self):
        from ferdim.ratelimit import TokenBucket
        b = TokenBucket(1000)                 # 1 KB/sn
        stop = threading.Event()
        out = []
        t = threading.Thread(target=lambda: out.append(b.take(10_000_000, stop)))
        t.start()
        time.sleep(0.3)
        stop.set()
        t.join(3)
        self.assertEqual(out, [False])        # bekleyis kesildi, bayt harcanmadi
        self.assertFalse(t.is_alive())
        self.assertTrue(b.take(1))
        self.assertTrue(TokenBucket(0).take(10 ** 9, stop))   # sinirsiz: hep True
        self.assertTrue(b.take(500))          # stop olsa da yer varsa alinir

    def test_11b_error_text_is_human_readable(self):
        """Ham kitaplik metni ('HTTP Error 404: File not found') yerine
        durumun ne oldugunu soyleyen kisa metin."""
        import urllib.error
        d = job_mod._describe_error
        self.assertEqual(d(urllib.error.HTTPError("u", 404, "File not found", {}, None)),
                         "kaynak bulunamadi (HTTP 404)")
        self.assertIn("503", d(urllib.error.HTTPError("u", 503, "x", {}, None)))
        self.assertEqual(d(TimeoutError("timed out")), "baglanti zaman asimi")
        ref = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        self.assertIn("sunucuya baglanilamadi", d(ref))
        self.assertEqual(d(RuntimeError("ozel durum")), "ozel durum")
        # gercek akista: olmayan dosya isi kisa metinle biter
        job = self._job(f"{self.base}/yok-bir-dosya.bin")
        job.start()
        self.assertTrue(wait_state(job, (JobState.ERROR,)))
        self.assertEqual(job.error, "kaynak bulunamadi (HTTP 404)")

    def test_12_primary_restart_is_bounded(self):
        """Tutarsiz birincil kaynak (surekli degisen) sonsuz restart
        dongusune yol acmamali."""
        job = self._job(f"{self.base}/file.bin")
        job.total = len(DATA)
        job._fd = os.open(self.env.download_dir / "restart.part",
                          os.O_RDWR | os.O_CREAT, 0o644)
        job._spawn = lambda: None          # deneysel: isci acma
        for _ in range(4):
            job._do_restart()
        self.assertEqual(job.state, JobState.ERROR)
        self.assertIn("degisiyor", job.error)
        job.cancel(delete_files=True)


class ChecksumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()
        cls.env = AppEnv(root=Path(tempfile.mkdtemp(prefix="ferdim-chk-")))
        cls.cfg = cls.env.load_config()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _run(self, url, **kw):
        job = DownloadJob(self.env, self.cfg, JobSpec(url=url, **kw))
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                        f"{job.state} {job.error}")
        return job

    def test_01_more_digests_ok(self):
        cfg = dict(self.cfg)
        cfg["checksum_sidecar"] = False
        job = DownloadJob(self.env, cfg, JobSpec(
            url=f"{self.base}/tiny.bin",
            checksums={"sha1": hashlib.sha1(TINY).hexdigest(),
                       "sha512": hashlib.sha512(TINY).hexdigest()}))
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sorted(job.snapshot()["verified"]), ["sha1", "sha512"])

    def test_02_more_digests_mismatch_fails(self):
        job = self._run(f"{self.base}/tiny.bin",
                        checksums={"sha512": "f" * 128})
        self.assertEqual(job.state, JobState.ERROR)
        self.assertIn("sha512", job.error)
        self.assertEqual(job.snapshot()["checksum"], "mismatch")
        job.cancel(delete_files=True)

    def test_03_sidecar_discovered_and_verified(self):
        """Acikca saglama verilmemis is, sunucudaki .sha256 yan dosyasini
        bulup kendini dogrular."""
        job = self._run(f"{self.base}/file.bin", segments=4)
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.snapshot()["checksum"], "ok")
        self.assertEqual(job.snapshot()["verified"], ["sha256"])
        self.assertEqual(job._sidecar, {"sha256": DATA_SHA})

    def test_04_sidecar_binary_format(self):
        job = self._run(f"{self.base}/tiny.bin")
        self.assertEqual(job.snapshot()["checksum"], "ok")

    def test_05_stale_sidecar_warns_but_keeps_file(self):
        """Bayat yan dosya uyusmazsa dosya yerinde kalir, is UYARI ile
        isaretlenir (hata sayilmaz)."""
        job = self._run(f"{self.base}/file2.bin", segments=4)
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.snapshot()["checksum"], "warn")
        self.assertTrue(job.final_path.exists())
        self.assertEqual(job.snapshot()["verified"], [])

    def test_06_no_sidecar_no_verify(self):
        job = self._run(f"{self.base}/norange.bin")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.snapshot()["checksum"], "")
        self.assertEqual(job._sidecar, {})

    def test_07_sidecar_can_be_disabled(self):
        cfg = dict(self.cfg)
        cfg["checksum_sidecar"] = False
        job = DownloadJob(self.env, cfg, JobSpec(url=f"{self.base}/tiny.bin"))
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job._sidecar, {})
        self.assertIsNone(job._sidecar_t)

    def test_08_explicit_beats_sidecar(self):
        """Ayni algoritma acikca istendiyse bayat yan dosya yok sayilir."""
        job = self._run(f"{self.base}/file2.bin", segments=2,
                        checksums={"md5": hashlib.md5(DATA).hexdigest()})
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.snapshot()["checksum"], "ok")
        self.assertEqual(job.snapshot()["verified"], ["md5"])

    def test_09_parse_checksum_text(self):
        core = f"{DATA_SHA}  dosya.bin\n"
        self.assertEqual(util.parse_checksum_text(core, "sha256", "dosya.bin"),
                         DATA_SHA)
        self.assertEqual(util.parse_checksum_text(core, "sha256"), DATA_SHA)
        self.assertEqual(
            util.parse_checksum_text(f"SHA256 (dosya.bin) = {DATA_SHA}", "sha256",
                                     "dosya.bin"), DATA_SHA)
        sums = (f"{'a' * 64}  baska.iso\n{DATA_SHA}  dosya.bin\n")
        self.assertEqual(util.parse_checksum_text(sums, "sha256", "dosya.bin"),
                         DATA_SHA)
        # eslesen dosya adi yoksa cok kayitli dosyadan ozet alinmaz
        self.assertEqual(util.parse_checksum_text(sums, "sha256", "yok.iso"), "")
        # HTML sayfalar / bozuk metinler guvenli sekilde reddedilir
        self.assertEqual(util.parse_checksum_text("<html>x</html>", "sha256"), "")
        self.assertEqual(util.parse_checksum_text("z" * 64, "sha256"), "")
        self.assertEqual(util.parse_checksum_text(DATA_SHA, "md5"), "")
        # 40'lik ozet, 64'luk ozetin icinden 'kesilmez'
        self.assertEqual(util.parse_checksum_text(DATA_SHA, "sha1"), "")

    def test_10_normalize_rejects_unknown(self):
        got = util.normalize_checksums({
            "sha256": "zz", "md5": "0" * 32, "SHA-512": "F" * 128,
            "bogus": "x", "sha1": "a" * 39})
        self.assertEqual(got, {"md5": "0" * 32, "sha512": "f" * 128})
        self.assertEqual(util.file_digests("/yok", ["bogus"]), {})
        self.assertEqual(util.url_sibling("http://x/f.iso?q=1#z", ".sha256"),
                         "http://x/f.iso.sha256?q=1#z")


    # --- saglama, is BITMIS sayilmadan once yapilmalidir (yaris) ---
    def test_11_verify_runs_before_done_is_published(self):
        """'done' durumunu goren herkes (CLI --wait, eklenti, web) dosyanin
        dogrulanmis oldugunu bilmeli: _verify sirasinda durum DONE olamaz."""
        seen = []
        orig = DownloadJob._verify

        def spy(job, dest, found=None):
            seen.append(job.state)
            return orig(job, dest, found)

        with mock.patch.object(DownloadJob, "_verify", spy):
            job = self._run(f"{self.base}/file.bin", segments=4, sha256=DATA_SHA)
        self.assertEqual(seen, [JobState.DOWNLOADING])
        self.assertEqual(job.state, JobState.DONE)
        self.assertEqual(job.snapshot()["checksum"], "ok")
        self.assertFalse(job.snapshot()["verifying"])

    def test_12_slow_sidecar_is_waited_for(self):
        """Yan dosya aramasi indirmeden uzun surerse bitis bir sure bekler;
        uyari 'done' dan once hazirdir."""
        real = httpcli.fetch_small

        def slow(url, opt, **kw):
            time.sleep(0.4)
            return real(url, opt, **kw)

        with mock.patch.object(httpcli, "fetch_small", slow):
            job = self._run(f"{self.base}/file2.bin", segments=4)
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(job.snapshot()["checksum"], "warn")

    def test_13_late_sidecar_is_verified_after_done(self):
        """Bekleme suresi doldugunda is 'done' olur; daha sonra bulunan yan
        dosya yine degerlendirilir (uyari kaybolmaz, hataya cevrilmez)."""
        real = httpcli.fetch_small
        gate = threading.Event()

        def late(url, opt, **kw):
            gate.wait(5)
            return real(url, opt, **kw)

        with mock.patch.object(job_mod, "SIDECAR_WAIT", 0.05), \
                mock.patch.object(httpcli, "fetch_small", late):
            job = self._run(f"{self.base}/file2.bin", segments=4)
            self.assertEqual(job.state, JobState.DONE, job.error)
            self.assertEqual(job.snapshot()["checksum"], "")   # henuz yok
            gate.set()
            t0 = time.monotonic()
            while time.monotonic() - t0 < 5 and \
                    not job.snapshot()["checksum"]:
                time.sleep(0.05)
        self.assertEqual(job.snapshot()["checksum"], "warn")
        self.assertEqual(job.state, JobState.DONE)              # hata degil
        self.assertEqual(job.snapshot()["verified"], [])

    def test_14_verify_late_unit(self):
        job = self._run(f"{self.base}/tiny.bin")
        good = hashlib.md5(TINY).hexdigest()
        job._verify_late({"md5": good})
        self.assertEqual(job.snapshot()["checksum"], "ok")
        self.assertIn("md5", job.snapshot()["verified"])
        job._verify_late({"md5": "0" * 32})
        self.assertEqual(job.snapshot()["checksum"], "warn")
        self.assertNotIn("md5", job.snapshot()["verified"])
        job.state = JobState.ERROR                  # hatali ise dokunulmaz
        job.extra["checksum"] = "mismatch"
        job._verify_late({"md5": good})
        self.assertEqual(job.extra["checksum"], "mismatch")


def fresh_env(prefix="ferdim-q-") -> AppEnv:
    """Her testin kendi kuyruk dosyini almasi icin temiz kok."""
    return AppEnv(root=Path(tempfile.mkdtemp(prefix=prefix)))


class QueueRoadmapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_01_persistence_roundtrip(self):
        """Ayna, saglama ve hiz limiti daemon yeniden baslasa da kaybolmaz."""
        env = fresh_env()
        cfg = env.load_config()
        cfg["max_concurrent"] = 1
        cfg["schedule"] = {"enabled": True, "start": "00:00", "stop": "00:00"}
        qm = QueueManager(env, cfg)
        try:
            job = qm.add(JobSpec(url=f"{self.base}/file.bin", segments=6,
                                 mirrors=[f"{self.base}/lazy.bin"],
                                 sha256=DATA_SHA, md5="0" * 32,
                                 checksums={"sha1": "a" * 40},
                                 limit=1_000_000, directory=str(env.download_dir),
                                 filename="kalici.bin"))
            qm._save()
        finally:
            qm.shutdown()
        qm2 = QueueManager(env, cfg)
        try:
            spec = qm2.get(job.id).spec
            self.assertEqual(spec.mirrors, [f"{self.base}/lazy.bin"])
            self.assertEqual(spec.wanted(), {"sha256": DATA_SHA, "md5": "0" * 32,
                                             "sha1": "a" * 40})
            self.assertEqual(spec.limit, 1_000_000)
            self.assertEqual(spec.filename, "kalici.bin")
            # kayit: baslamadan duraklatildi -> devam edebilir
            self.assertIn(qm2.get(job.id).state, (JobState.PAUSED, JobState.QUEUED))
        finally:
            qm2.shutdown()

    def test_02_category_window_holds_only_that_category(self):
        now = _dt.datetime.now()

        def hm(delta_h):
            return (now + _dt.timedelta(hours=delta_h)).strftime("%H:%M")

        closed_w = {"start": hm(2), "stop": hm(3)}
        env = fresh_env()
        cfg = env.load_config()
        cfg["max_concurrent"] = 4
        cfg["smart_queue"] = False
        cfg["schedule"] = {"enabled": False, "categories": {
            "compressed": closed_w,
            "document": {"start": hm(-1), "stop": hm(1)}}}
        qm = QueueManager(env, cfg)
        try:
            held = qm.add(JobSpec(url=f"{self.base}/tiny.bin", filename="paket.zip"))
            free = qm.add(JobSpec(url=f"{self.base}/tiny.bin", filename="not.txt"))
            qm._tick()
            time.sleep(0.3)
            self.assertEqual(held.state, JobState.QUEUED, "pencere kapali: beklemeli")
            self.assertNotEqual(free.state, JobState.QUEUED, "pencere acik: baslamali")
            snap = held.snapshot()
            self.assertTrue(snap["sched_wait"])
            self.assertEqual(snap["window"], f"{closed_w['start']}-{closed_w['stop']}")
            self.assertFalse(free.snapshot()["sched_wait"])
        finally:
            qm.shutdown()

    def test_02b_job_keeps_its_folder_when_default_changes(self):
        """Varsayilan indirme klasoru degisince eskiden eklenmis islerin
        yolu da kayiyordu (bitmis is baska klasorde gosteriliyor,
        yeniden yuklenince yeniden kuyruga giriyordu). Hedef klasor is
        eklenirken sabitlenir ve kuyruk dosyasina yazilir."""
        env = AppEnv.__new__(AppEnv)            # gercek koklu AppEnv el ile
        env._fixed_dir = False
        env._download_dir = None
        env._dir_cfg = None
        env.state_dir = Path(tempfile.mkdtemp(prefix="ferdim-dir-"))
        env.jobs_dir = env.state_dir / "jobs"
        env.jobs_dir.mkdir(parents=True)
        env.config_path = env.state_dir / "config.json"
        a, b = env.state_dir / "a", env.state_dir / "b"
        cfg = {"download_dir": str(a), "turbo_mode": False}
        env.reload_download_dir(cfg)             # daemon'in yaptigi gibi
        qm = QueueManager(env, cfg)
        try:
            old = qm.add(JobSpec(url="http://127.0.0.1:1/eski.bin"), paused=True)
            self.assertEqual(old.target_dir, a)
            cfg["download_dir"] = str(b)
            env.reload_download_dir(cfg)       # ayar canli degisti
            self.assertEqual(old.target_dir, a)          # eski is yerinde
            new = qm.add(JobSpec(url="http://127.0.0.1:1/yeni.bin"), paused=True)
            self.assertEqual(new.target_dir, b)          # yeni is yeni klasore
            self.assertEqual(old.snapshot()["dir"], str(a))
            qm._save()
            qm2 = QueueManager(env, cfg)                  # daemon yeniden basladi
            try:
                self.assertEqual(qm2.get(old.id).target_dir, a)
                self.assertEqual(qm2.get(new.id).target_dir, b)
            finally:
                qm2.shutdown()
        finally:
            qm.shutdown()

    def test_03_window_open_logic(self):
        noon = _dt.datetime(2026, 5, 1, 12, 0)
        self.assertFalse(util.window_open("02:00", "08:00", noon))
        self.assertTrue(util.window_open("10:00", "13:00", noon))
        self.assertTrue(util.window_open("23:00", "05:00", _dt.datetime(2026, 5, 1, 3, 0)))
        self.assertFalse(util.window_open("23:00", "05:00", noon))
        self.assertTrue(util.window_open("bozuk", "veri", noon), "gecersiz giris = sinirsiz")
        self.assertTrue(util.window_open("", "", noon))

    def test_04_watch_folder_queues_and_consumes(self):
        env = fresh_env()
        cfg = env.load_config()
        cfg["max_concurrent"] = 2
        qm = QueueManager(env, cfg)
        ctx = {"qm": qm, "env": env, "cfg": cfg, "set_cfg": env.save_config}
        d = Path(tempfile.mkdtemp(prefix="ferdim-watch-"))
        xml = f"""<?xml version="1.0"?><metalink version="3.0"
         xmlns="urn:ietf:params:xml:ns:metalink"><file name="tiny.metalink.bin">
         <hash type="sha-256">{hashlib.sha256(TINY).hexdigest()}</hash>
         <url preference="10">{self.base}/tiny.bin</url>
         <url preference="20">{self.base}/tiny.bin?m=2</url></file></metalink>"""
        (d / "links.txt").write_text(
            "# baglantilar\n\n" + f"{self.base}/tiny.bin?u=1\n; yorum\n"
            f"{self.base}/tiny.bin?u=1\n" + f"{self.base}/tiny.bin?u=2\n", "utf-8")
        (d / "liste.metalink").write_text(xml, "utf-8")
        (d / "yaris.txt").write_text(f"{self.base}/tiny.bin\n", "utf-8")
        for f in d.iterdir():
            os.utime(f, (time.time() - 30,) * 2)
        # 'yaris.txt' hala yaziliyor sayilir: mtime simdi
        os.utime(d / "yaris.txt", None)
        cfg["watch_dirs"] = [str(d)]
        cfg["watch_done"] = "delete"
        w = FolderWatcher(qm, cfg, env,
                          lambda p: api.handle(ctx, "add-file", {"path": p}))
        try:
            self.assertEqual(w.poll(), 2)
            self.assertEqual(len(qm.jobs), 3, "2 URL + 1 metalink")
            ml = next(j for j in qm.jobs if j.spec.mirrors)
            self.assertEqual(len(ml.spec.mirrors), 1)
            self.assertEqual(ml.spec.sha256, hashlib.sha256(TINY).hexdigest())
            self.assertEqual(ml.final_filename, "tiny.metalink.bin")
            self.assertFalse((d / "links.txt").exists(), "kuyruga alinan dosya silinmeli")
            self.assertFalse((d / "liste.metalink").exists())
            self.assertTrue((d / "yaris.txt").exists(), "yeni yazilan dosya dokunulmamali")
            self.assertEqual(w.poll(), 0, "ayni dosya iki kez eklenmemeli")
        finally:
            qm.shutdown()

    def test_05_watch_folder_keep_mode(self):
        env = fresh_env()
        cfg = env.load_config()
        cfg["max_concurrent"] = 1
        qm = QueueManager(env, cfg)
        ctx = {"qm": qm, "env": env, "cfg": cfg, "set_cfg": env.save_config}
        d = Path(tempfile.mkdtemp(prefix="ferdim-watch2-"))
        f = d / "x.urls"
        f.write_text(f"{self.base}/tiny.bin?k=1\n", "utf-8")
        os.utime(f, (time.time() - 30,) * 2)
        cfg["watch_dirs"] = [str(d)]
        cfg["watch_done"] = "keep"
        w = FolderWatcher(qm, cfg, env,
                          lambda p: api.handle(ctx, "add-file", {"path": p}))
        try:
            self.assertEqual(w.poll(), 1)
            self.assertTrue(f.exists(), "keep: dosya yerinde kalir")
            self.assertEqual(w.poll(), 0, "ayni dosya tekrar eklenmez")
            # cozulemeyen dosya birakilir ve bu oturumda tekrar denenmez
            bad = d / "cop.txt"
            bad.write_text("burada url yok\n", "utf-8")
            os.utime(bad, (time.time() - 30,) * 2)
            self.assertEqual(w.poll(), 0)
            self.assertTrue(bad.exists())
            self.assertEqual(w.poll(), 0)
        finally:
            qm.shutdown()

    def test_06_parse_url_list(self):
        text = ("# y\n\nhttp://a/x.iso\nhttps://b/y.iso, \n; yorum\n"
                "// yorum\nftp://c/z\nhttp://a/x.iso\n"
                "aria2c -x8 https://d/w(v).zip)\nhttps://e/end.")
        self.assertEqual(parse_url_list(text), [
            "http://a/x.iso", "https://b/y.iso", "ftp://c/z",
            "https://d/w(v).zip", "https://e/end"])
        self.assertEqual(len(parse_url_list("\n".join(f"http://x/{i}" for i in range(50)),
                                            limit=10)), 10)

    def test_07_add_file_api(self):
        env = fresh_env()
        cfg = env.load_config()
        qm = QueueManager(env, cfg)
        ctx = {"qm": qm, "env": env, "cfg": cfg, "set_cfg": env.save_config}
        try:
            r = api.handle(ctx, "add-file", {"path": "/yok/bir/dosya.txt"})
            self.assertFalse(r["ok"])
            d = Path(tempfile.mkdtemp(prefix="ferdim-addfile-"))
            f = d / "l.txt"
            f.write_text(f"{self.base}/tiny.bin?a=1\nnot a url\n", "utf-8")
            r = api.handle(ctx, "add-file", {"path": str(f)})
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["count"], 1)
            self.assertEqual(len(r["ids"]), 1)
            big = d / "big.txt"
            big.write_bytes(b"x" * 100)
            self.assertFalse(api.handle(ctx, "add-file", {"path": str(big)})["ok"])
        finally:
            qm.shutdown()


class ApiBehaviorTests(unittest.TestCase):
    """1.4.0'da bulunan kusurlarin duzeltmeleri: duraklatili eklenen is,
    gecersiz saglamanin reddi, turbo/limit uyumu, indirme dizini degisimi."""

    def setUp(self):
        self.env = fresh_env("ferdim-api-")
        self.cfg = self.env.load_config()
        self.qm = QueueManager(self.env, self.cfg)
        self.ctx = {"qm": self.qm, "env": self.env, "cfg": self.cfg,
                    "set_cfg": self.env.save_config}

    def tearDown(self):
        self.qm.shutdown()

    def test_01_add_paused_is_not_started_by_tick(self):
        r = api.handle(self.ctx, "add", {"url": "http://127.0.0.1:1/x.bin",
                                         "paused": True})
        self.assertTrue(r["ok"], r)
        job = self.qm.get(r["id"])
        self.assertEqual(job.state, JobState.PAUSED)
        for _ in range(3):
            self.qm._tick()
            time.sleep(0.05)
        self.assertEqual(job.state, JobState.PAUSED)
        # isaret kalici: yeniden yuklense de duraklatilmis kalir
        qm2 = QueueManager(self.env, self.cfg)
        try:
            self.assertEqual(qm2.get(r["id"]).state, JobState.PAUSED)
        finally:
            qm2.shutdown()

    def test_02_invalid_checksum_is_rejected_not_ignored(self):
        url = "http://127.0.0.1:1/x.bin"
        for bad in ({"checksum": "zzz"}, {"checksum": "sha256:abc"},
                    {"sha256": "abc"}, {"md5": "12345"},
                    {"sha512": "q" * 128}, {"sha1": "f" * 39}):
            r = api.handle(self.ctx, "add", {"url": url, **bad})
            self.assertFalse(r["ok"], bad)
        self.assertEqual(self.qm.jobs, [])
        good = api.handle(self.ctx, "add", {
            "url": url, "checksum": "sha256:" + "a" * 64,
            "sha512": "B" * 128})
        self.assertTrue(good["ok"], good)
        spec = self.qm.get(good["id"]).spec
        self.assertEqual(spec.sha256, "a" * 64)
        self.assertEqual(spec.wanted(), {"sha256": "a" * 64,
                                         "sha512": "b" * 128})

    def test_03_speed_limit_leaves_turbo(self):
        self.assertTrue(self.cfg["turbo_mode"])
        r = api.handle(self.ctx, "limit", {"value": 2_000_000})
        self.assertEqual(r["speed_limit"], 2_000_000)
        self.assertFalse(self.cfg["turbo_mode"])      # buton ile limit uyusur
        self.assertEqual(self.qm.stats()["turbo"], False)
        self.assertEqual(api.handle(self.ctx, "limit", {"value": "3MB"})
                         ["speed_limit"], 3 * 1024 ** 2)

    def test_03b_parse_size_roundtrips_with_display(self):
        """Girilen hiz ekranda ayni sayiyla geri gorunmelidir."""
        for text, shown in (("2MB", "2.0 MB"), ("512kb", "512.0 KB"),
                            ("1.5m", "1.5 MB"), ("2 MiB", "2.0 MB"),
                            ("1gib", "1.0 GB"), ("3G", "3.0 GB")):
            n = util.parse_size(text)
            self.assertTrue(util.human_size(n).startswith(shown), (text, n))
        self.assertEqual(util.parse_size("1048576"), 1 << 20)
        self.assertEqual(util.parse_size("0"), 0)
        self.assertEqual(util.parse_size(None), 0)
        self.assertEqual(util.parse_size("abc"), 0)
        self.assertEqual(util.parse_size(""), 0)

    def test_04_concurrency_set_during_turbo_survives_turbo_off(self):
        self.qm.set_turbo(True)
        r = api.handle(self.ctx, "config-set",
                       {"config": {"max_concurrent": 5}})
        self.assertTrue(r["ok"], r)
        self.assertGreaterEqual(self.cfg["max_concurrent"], 5)   # turbo sisirir
        self.qm.set_turbo(False)
        self.assertEqual(self.cfg["max_concurrent"], 5)          # kullanici secimi

    def test_05_config_set_roundtrip_new_keys(self):
        r = api.handle(self.ctx, "config-set", {"config": {
            "download_dir": None, "segments": 16, "max_concurrent": 3,
            "language": "en",
            "schedule": {"enabled": True, "start": "01:00", "stop": "06:00",
                         "categories": {"video": {"start": "2:00",
                                                  "stop": "08:00"}}},
            "watch_dirs": [str(self.env.state_dir), " ", 5],
            "watch_done": "rename", "checksum_sidecar": False,
            "retries": 9, "timeout": 40, "stall_timeout": 45}})
        self.assertTrue(r["ok"], r)
        self.assertNotIn("warning", r)
        c = r["config"]
        self.assertEqual(c["schedule"]["categories"]["video"]["start"], "2:00")
        self.assertEqual(c["watch_dirs"], [str(self.env.state_dir)])
        self.assertEqual(c["watch_done"], "rename")
        self.assertFalse(c["checksum_sidecar"])
        self.assertEqual((c["retries"], c["timeout"]), (9, 40))
        self.assertEqual(c["stall_timeout"], 40)      # baglanti asimi siniri
        self.assertEqual(self.env.load_config()["segments"], 16)

    def test_06_download_dir_follows_setting(self):
        # gercek koklu AppEnv (root'suz) kullanilmaz: alanlar elle kurulur
        env = AppEnv.__new__(AppEnv)
        env._fixed_dir = False
        env._download_dir = None
        env._dir_cfg = None
        d = Path(tempfile.mkdtemp(prefix="ferdim-dl-"))
        env.config_path = d / "config.json"
        one, two = d / "bir", d / "iki"
        env.config_path.write_text(json.dumps({"download_dir": str(one)}))
        self.assertEqual(env.download_dir, one)
        env.config_path.write_text(json.dumps({"download_dir": str(two)}))
        self.assertEqual(env.download_dir, one)       # onbellek (eski kusur)
        env.reload_download_dir()
        self.assertEqual(env.download_dir, two)
        # acilamayan dizin (bir dosyanin altinda yol): varsayilana doner
        blocker = d / "dosya"
        blocker.write_text("x")
        cfg = {"download_dir": str(blocker / "alt")}
        ctx = {"env": env, "cfg": cfg}
        env.config_path.write_text(json.dumps({"download_dir": str(d)}))
        self.assertEqual(api._recheck_download_dir(ctx), "download_dir")
        self.assertIsNone(cfg["download_dir"])
        # None -> sistem indirme klasoru; dosyadaki (gecerli) deger sayilmaz
        self.assertEqual(env.download_dir, paths.default_download_dir())
        cfg["download_dir"] = str(two)                 # yeni deger canli okunur
        env.reload_download_dir(cfg)
        self.assertEqual(env.download_dir, two)

    def test_07_test_root_dir_is_never_reloaded(self):
        before = self.env.download_dir
        self.ctx["cfg"]["download_dir"] = "/yok/bir/dizin"
        api._recheck_download_dir(self.ctx)
        self.assertEqual(self.env.download_dir, before)


class UiStaticTests(unittest.TestCase):
    """Web arayuzu ve GTK kaynaklarinin statik denetimi (tarayici gerekmez)."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent.parent

    def test_01_web_has_no_innerhtml_with_server_data(self):
        js = (self.root / "web" / "app.js").read_text("utf-8")
        self.assertNotIn("innerHTML", js, "sunucu verisi DOM'a textContent ile girmeli")

    def test_02_web_a11y_landmarks(self):
        html = (self.root / "web" / "index.html").read_text("utf-8")
        for needle in ('role="tablist"', 'role="tab"', 'role="table"',
                       'class="skip"', 'aria-live="polite"', 'aria-labelledby'):
            self.assertIn(needle, html, needle)
        # title icinde etiket olmaz (kapanis "</title>" haric)
        self.assertNotRegex(html, r"<title>[^<]*<(?!/title>)",
                            "title icinde etiket olmaz")
        css = (self.root / "web" / "style.css").read_text("utf-8")
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn(":focus-visible", css)
        self.assertIn(".sr-only", css)

    def test_03_i18n_keys_used_by_ui_exist(self):
        js = (self.root / "web" / "app.js").read_text("utf-8")
        html = (self.root / "web" / "index.html").read_text("utf-8")
        gui = (self.root / "ferdim" / "gui" / "app.py").read_text("utf-8")
        # tam anahtar: tr("anahtar") / trf("anahtar", ...); bitisik
        # anahtar tamlayan tr("cat_" + x) ve st_* dinamikleri sayilmaz
        used = set(re.findall(r'\btrf?\("([a-z_0-9]+)"\s*[,)]', js))
        used |= set(re.findall(r'data-i18n(?:-ph|-aria|-title)?="([a-z_0-9]+)"',
                              html))
        used |= set(re.findall(r'\bt\(["\']([a-z_0-9]+)["\']\s*[,)]', gui))
        missing = sorted(k for k in used if k not in S and not k.startswith("st_"))
        self.assertEqual(missing, [], "eksik i18n anahtarlari")

    def test_04_new_i18n_keys_in_all_languages(self):
        for key in ("chk_ok", "chk_warn", "mirrors_dead", "sched_wait",
                    "lbl_watch_dirs", "lbl_watch_done", "watch_delete",
                    "lbl_sidecar", "lbl_cat_sched", "daemon_lost", "lbl_mirrors",
                    "lbl_checksum"):
            self.assertIn(key, S, key)
            self.assertEqual(set(S[key]), {"en", "tr", "es", "ru"}, key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
