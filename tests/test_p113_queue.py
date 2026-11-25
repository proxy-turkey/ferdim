"""Faz 1.13: kuyruk olcegi - kalicilik, stats deltalari, denetim, yoklama.

Pinlenen davranis:

- Kalicilik: tek is ekleme hemen yazilir; toplu ekleme (add-batch / URL
  listesi / metalink) TEK dosya yazimi eder. Durum degisiklikleri kirli
  bayrak + birlesen yazici ile yazilir; shutdown son durumu eszamanli
  yazar. Kayit listesi kuyruk kilidi altinda serilestirilmez.
- stats(): is sozlukleri kilit DISINDA uretilir; `since` revizyon deltasi,
  `limit`/`offset` sayfalama, `removed` silinen kimlikler, `full` bayragi.
- Denetim: HTTP istekleri (tarayici) beklemez, is kuyrugunda sirayla
  yurutulur; soket istemcisi (CLI/GTK) sonucu bekler.
- Boyut yoklayicisi: en fazla 3 es zamanli, silinen isi unutur, bos
  kuyrukta tarama yapmaz.
- util.local_zone(): TZ degisince onbellek gecersiz olur.

Testler sabit uyku ile zamanlama yapmaz: kapi (Event) ve son tarihli
yoklama kullanilir.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import api, util
from ferdim.engine import queue as queue_mod
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.engine.queue import QUEUE_FILE, QueueManager
from ferdim.paths import AppEnv
from tests.helpers import tmp_root

URL = "http://127.0.0.1:1/dosya.bin"


def wait_until(pred, timeout=10.0):
    """Son tarihli yoklama (sabit uyku yok)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


class QueueCase(unittest.TestCase):
    """Ortak kurulum: arka plan donguleri kapali, ag erisimi yok."""

    def _queue(self, prober=False, saver=False, **over) -> QueueManager:
        self.env = AppEnv(root=tmp_root(self, "ferdim-p113-"))
        self.cfg = self.env.load_config()
        self.cfg.update({"max_concurrent": 0, "smart_queue": False,
                         "retries": 0, "timeout": 1, "schedule": {}})
        self.cfg.update(over)
        patches = [
            # tick elle denetlenir (ag yok); yazici varsayilan kapali
            mock.patch.object(QueueManager, "_tick_loop", lambda self: None),
        ]
        if not saver:
            patches.append(mock.patch.object(QueueManager, "_save_loop",
                                             lambda self: None))
        if not prober:
            patches.append(mock.patch.object(QueueManager, "_prober_loop",
                                             lambda self: None))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        qm = QueueManager(self.env, self.cfg)
        self.addCleanup(qm.shutdown)
        return qm

    def _ctx(self, qm: QueueManager, transport: str | None = None) -> dict:
        ctx = {"qm": qm, "env": qm.env, "cfg": qm.cfg,
               "set_cfg": qm.env.save_config}
        if transport:
            ctx["transport"] = transport
        return ctx

    def _add(self, qm: QueueManager, url: str = URL, paused: bool = True):
        return qm.add(JobSpec(url=url), paused=paused)

    def _raw_queue(self) -> list:
        return json.loads((self.env.state_dir / QUEUE_FILE).read_text("utf-8"))


class PersistenceTests(QueueCase):
    def test_add_batch_of_500_writes_the_queue_once(self):
        """500 islik ice aktarma O(n^2) yazim yapmamali: tek dosya yazimi."""
        qm = self._queue()
        items = [{"url": f"http://127.0.0.1:1/dosya{i}.bin", "paused": True}
                 for i in range(500)]
        writes = []
        real = util.atomic_write_json

        def counting(path, data):
            writes.append(len(data))
            real(path, data)

        with mock.patch.object(util, "atomic_write_json", counting):
            r = api.handle(self._ctx(qm), "add-batch", {"items": items})

        self.assertTrue(r.get("ok"), r)
        self.assertEqual(len(r["ids"]), 500)
        self.assertEqual(len(writes), 1, f"500 is icin {len(writes)} yazim")
        self.assertEqual(writes[0], 500, "tek yazim tum kuyrugu tasimali")
        self.assertEqual(len(self._raw_queue()), 500)

    def test_first_add_writes_and_later_adds_are_debounced(self):
        """P5: tek ekleme debonse girer; ilk is (dosya yokken) hemen yazilir."""
        qm = self._queue()
        first = self._add(qm)
        self.assertEqual([r["id"] for r in self._raw_queue()], [first.id])
        writes = []
        real = util.atomic_write_json

        def counting(path, data):
            writes.append(len(data))
            real(path, data)

        with mock.patch.object(util, "atomic_write_json", counting):
            second = self._add(qm, "http://127.0.0.1:1/ikinci.bin")
        self.assertEqual(writes, [], "tek ekleme kuyrugu hemen yazdi")
        self.assertTrue(qm._dirty, "ekleme kirli bayragi isaretlemeli")
        qm.flush()
        self.assertEqual([r["id"] for r in self._raw_queue()],
                         [first.id, second.id])

    def test_state_change_is_persisted_by_the_debounced_saver(self):
        """M3: durum gecisi tick'i beklemez, 0.5 sn pencerede diske girer."""
        qm = self._queue(saver=True)
        job = self._add(qm)
        qm.flush()                      # baslangic temiz
        self.assertFalse(qm._dirty)
        job.state = JobState.ERROR
        job.error = "agnet"
        qm._job_changed(job)            # tick yok: yalniz bildirim
        self.assertTrue(qm._dirty, "bildirim kirli bayragi isaretlemeli")
        path = self.env.state_dir / QUEUE_FILE

        def persisted():
            try:
                recs = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                return False
            return any(r["id"] == job.id and r["state"] == JobState.ERROR
                       and r["error"] == "agnet" for r in recs)

        self.assertTrue(wait_until(persisted, 5),
                        "debounce penceresinde yazilmadi")

    def test_shutdown_waits_for_a_control_op_that_finishes_late(self):
        """M2: ilk join penceresinden SONRA biten silme son yazima girer."""
        qm = self._queue()
        job = self._add(qm)
        entered = threading.Event()
        real_remove = qm.remove

        def slow_remove(job_id, delete_files=False):
            entered.set()
            time.sleep(0.05)            # ilk join penceresinden sonra biter
            return real_remove(job_id, delete_files)

        with mock.patch.object(qm, "remove", slow_remove), \
                mock.patch.object(queue_mod, "QUEUE_CTL_FIRST_WAIT", 0.0):
            self.assertTrue(qm.remove_async(job.id))
            self.assertTrue(entered.wait(5))
            qm.shutdown()
        self.assertIsNone(qm.get(job.id))
        self.assertEqual(self._raw_queue(), [],
                         "gec biten silme son yazima girmeliydi")

    def test_shutdown_does_not_hang_on_a_stuck_control_op(self):
        qm = self._queue()
        job = self._add(qm)
        entered, release = threading.Event(), threading.Event()
        real_remove = qm.remove

        def stuck_remove(job_id, delete_files=False):
            entered.set()
            release.wait(10)
            return real_remove(job_id, delete_files)

        try:
            with mock.patch.object(qm, "remove", stuck_remove), \
                    mock.patch.object(queue_mod, "QUEUE_CTL_FIRST_WAIT", 0.0), \
                    mock.patch.object(queue_mod, "QUEUE_CTL_TAIL_WAIT", 0.05):
                self.assertTrue(qm.remove_async(job.id))
                self.assertTrue(entered.wait(5))
                t0 = time.monotonic()
                qm.shutdown()
                self.assertLess(time.monotonic() - t0, 3.0,
                                "kapanis takildi")
        finally:
            release.set()
        self.assertTrue(qm._closed)

    def test_shutdown_persists_the_latest_state(self):
        # yazici is parcacigi kapali: shutdown'in eszamanli yazimi kanitlanir
        qm = self._queue()
        keep = [self._add(qm) for _ in range(3)]
        go = keep[1]
        self.assertTrue(qm.remove(go.id))
        qm.shutdown()
        self.assertEqual(sorted(r["id"] for r in self._raw_queue()),
                         sorted(j.id for j in (keep[0], keep[2])))
        # kapanistan sonra arka plan yazimi kuyrugu geri getirmez
        self.assertFalse(qm.flush())

    def test_unknown_fields_of_a_loaded_record_survive_a_rewrite(self):
        """Ileri surumden gelen bilinmeyen alanlar yeniden yazimda korunur."""
        qm = self._queue()
        job = self._add(qm)
        job._rec_extra = {"gelecek_alani": "korunur"}
        qm._save()
        rec = self._raw_queue()[0]
        self.assertEqual(rec["id"], job.id)
        self.assertEqual(rec["gelecek_alani"], "korunur")


class StatsTests(QueueCase):
    def _three(self, qm):
        return [self._add(qm, f"http://127.0.0.1:1/d{i}.bin")
                for i in range(3)]

    def test_full_response_has_all_keys_and_pages(self):
        qm = self._queue()
        a, b, c = self._three(qm)
        st = qm.stats()
        self.assertTrue(st["full"])
        self.assertEqual(st["removed"], [])
        self.assertEqual(st["total_jobs"], 3)
        self.assertGreaterEqual(st["rev"], 1)
        self.assertEqual([j["id"] for j in st["jobs"]], [a.id, b.id, c.id])

        page = qm.stats(limit=2, offset=1)
        self.assertTrue(page["full"])
        self.assertEqual([j["id"] for j in page["jobs"]], [b.id, c.id])
        self.assertEqual(page["total_jobs"], 3,
                         "sayfalama toplami degistirmez")

        # gecersiz/<=0 limit sinirsiz; offset negatifse 0
        self.assertEqual(len(qm.stats(limit=0)["jobs"]), 3)
        self.assertEqual(len(qm.stats(limit=-5)["jobs"]), 3)

    def test_active_and_speed_cover_all_jobs_not_just_the_page(self):
        qm = self._queue()
        a, b, c = self._three(qm)
        a.state = JobState.DOWNLOADING
        a.speed = 1500.0
        st = qm.stats(limit=1)
        self.assertEqual(len(st["jobs"]), 1)
        self.assertEqual(st["active"], 1)
        self.assertEqual(st["total_speed"], 1500)

    def test_delta_reports_changed_and_removed_only(self):
        qm = self._queue()
        a, b, c = self._three(qm)
        first = qm.stats()
        rev = first["rev"]

        # degisiklik yok: delta bos
        same = qm.stats(since=rev)
        self.assertFalse(same["full"])
        self.assertEqual(same["jobs"], [])
        self.assertEqual(same["removed"], [])
        self.assertEqual(same["rev"], rev)

        # tek is degisti: delta yalniz onu getirir
        b.state = JobState.DONE
        delta = qm.stats(since=rev)
        self.assertFalse(delta["full"])
        self.assertEqual([j["id"] for j in delta["jobs"]], [b.id])
        self.assertGreater(delta["rev"], rev)

        # silinen is 'removed' listesinde bildirilir
        qm.remove(c.id)
        after = qm.stats(since=delta["rev"])
        self.assertFalse(after["full"])
        self.assertEqual(after["removed"], [c.id])
        self.assertEqual(after["total_jobs"], 2)
        self.assertEqual(after["jobs"], [])

        # bir sonraki delta ayni silmeyi tekrar bildirmez
        again = qm.stats(since=after["rev"])
        self.assertEqual(again["removed"], [])

    def test_stale_or_future_since_returns_the_full_view(self):
        qm = self._queue()
        self._three(qm)
        rev = qm.stats()["rev"]
        future = qm.stats(since=rev + 1_000_000)
        self.assertTrue(future["full"], "daemon yeniden basladi: tam gorunum")
        self.assertEqual(len(future["jobs"]), 3)
        self.assertEqual(future["removed"], [])

    def test_tombstone_log_stays_bounded(self):
        """M2: silme gunlugu sinirlidir; eski since tam gorunum ister."""
        qm = self._queue()
        jobs = [self._add(qm, f"http://127.0.0.1:1/tb{i}.bin")
                for i in range(6)]
        with mock.patch.object(queue_mod, "QUEUE_TOMB_MAX", 3):
            for job in jobs:
                qm.remove(job.id)
            self.assertLessEqual(len(qm._removed), 3,
                                 "silme gunlugu sinirsiz buyudu")
        self.assertGreater(qm._tomb_floor, 0)
        st = qm.stats(since=0)
        self.assertTrue(st["full"], "gunluk tasdi: tam gorunum gerekli")
        self.assertEqual(st["removed"], [])

    def test_since_older_than_the_tombstone_window_is_full(self):
        qm = self._queue()
        jobs = [self._add(qm, f"http://127.0.0.1:1/s{i}.bin")
                for i in range(6)]
        rev = qm.stats()["rev"]
        with mock.patch.object(queue_mod, "QUEUE_TOMB_MAX", 4):
            for job in jobs:
                qm.remove(job.id)
        st = qm.stats(since=rev)
        self.assertTrue(st["full"], "silme gunlugu tasdi: tam gorunum")
        self.assertEqual(st["jobs"], [])
        self.assertEqual(st["removed"], [])

    def test_stats_does_not_hold_the_queue_lock_while_snapshotting(self):
        qm = self._queue()
        job = self._add(qm)
        real = job.snapshot
        started, release = threading.Event(), threading.Event()

        def blocking_snapshot(windows=None, copy=True):
            started.set()
            release.wait(5)
            return real()

        job.snapshot = blocking_snapshot
        out: dict = {}

        def run():
            out["st"] = qm.stats()

        th = threading.Thread(target=run)
        th.start()
        try:
            self.assertTrue(started.wait(5), "snapshot cagrilmadi")
            acquired = qm._lock.acquire(timeout=2)
            if acquired:
                qm._lock.release()
            self.assertTrue(acquired,
                            "stats kuyruk kilidini snapshot sirasinda tutuyor")
        finally:
            release.set()
            th.join(5)
        self.assertTrue(out.get("st"), "stats donmedi")
        self.assertEqual([j["id"] for j in out["st"]["jobs"]], [job.id])

    def test_snapshot_cache_reflects_direct_field_changes(self):
        """Onbellek TUM gorunen alanlari kapsar: alan dogrudan degisse de
        snapshot/stats taze deger dondurur (bayat gorunum olmaz)."""
        qm = self._queue()
        job = self._add(qm)
        first = qm.stats()["jobs"][0]
        self.assertEqual(first["state"], JobState.PAUSED)
        rev = qm.stats()["rev"]

        job.total = 4096
        job.done = 1024
        job.speed = 2048.0
        job._active = 3
        job.error = "agnet"
        job.error_code = "network"
        job.extra["checksum"] = "warn"
        job.extra["checksum_note"] = "sha256 uyusmuyor"
        job.extra["verified"] = ["sha256"]
        job.extra["verify_total"] = 9
        job.final_filename = "yeni-ad.bin"

        snap = job.snapshot()
        self.assertEqual((snap["size"], snap["done"], snap["speed"]),
                         (4096, 1024, 2048))
        self.assertEqual(snap["segments"], 3)
        self.assertEqual(snap["error"], "agnet")
        self.assertEqual(snap["error_code"], "network")
        self.assertEqual(snap["checksum"], "warn")
        self.assertEqual(snap["checksum_note"], "sha256 uyusmuyor")
        self.assertEqual(snap["verified"], ["sha256"])
        self.assertEqual(snap["verify_total"], 9)
        self.assertEqual(snap["filename"], "yeni-ad.bin")
        self.assertIn("checksum_mismatch", snap["warning_codes"])
        st = qm.stats()
        row = st["jobs"][0]
        self.assertEqual(row["filename"], "yeni-ad.bin")
        self.assertEqual(row["checksum"], "warn")
        self.assertGreater(st["rev"], rev, "icerik degisti: revizyon artmali")
        # ayni cagri tekrar: yeni revizyon yok
        self.assertEqual(qm.stats(since=st["rev"])["jobs"], [])

    def test_snapshot_cache_is_not_shared_with_callers(self):
        """Donen sozluk/listeler cagirana aittir: degistirmek onbellegi
        bozmaz (copy=True varsayilani)."""
        qm = self._queue()
        job = self._add(qm)
        snap = job.snapshot()
        snap["state"] = "bozuk"
        snap["warning_codes"].append("uydurma")
        snap["verified"].append("sahte")
        again = job.snapshot()
        self.assertEqual(again["state"], JobState.PAUSED)
        self.assertEqual(again["warning_codes"], [])
        self.assertEqual(again["verified"], [])

    def test_category_window_is_computed_once_per_stats_call(self):
        """stats: pencere kumesi cagri basina bir kez hesaplanir."""
        # .bin -> 'program' kategorisi: pencere o kategoriye tanimlanir
        qm = self._queue(schedule={
            "enabled": False, "start": "00:00", "stop": "00:00",
            "categories": {"program": {"start": "00:00", "stop": "00:01"}}})
        jobs = [self._add(qm, f"http://127.0.0.1:1/w{i}.bin")
                for i in range(4)]
        calls = []
        with mock.patch.object(util, "window_open",
                               lambda *a, **k: bool(calls.append(a))):
            st = qm.stats()
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(len(st["jobs"]), 4)
        for row in st["jobs"]:
            self.assertTrue(row["sched_wait"], row["window"])
            self.assertEqual(row["window"], "00:00-00:01")
        self.assertEqual({j.id for j in jobs},
                         {row["id"] for row in st["jobs"]})

    def test_api_parses_stats_parameters_defensively(self):
        qm = self._queue()
        a, b, c = self._three(qm)
        ctx = self._ctx(qm)
        st = api.handle(ctx, "stats", {"since": "abc", "limit": "2",
                                       "offset": "yo-k"})
        self.assertTrue(st["ok"])
        self.assertTrue(st["full"], "gecersiz since = tam gorunum")
        self.assertEqual(len(st["jobs"]), 2)
        self.assertEqual(st["total_jobs"], 3)
        # bilinmeyen anahtar yok sayilir: tam gorunum, limitsiz
        self.assertEqual(len(api.handle(ctx, "stats", {"since": object()})
                             ["jobs"]), 3)


    def test_unstable_queue_falls_back_to_a_full_view(self):
        """Kuyruk snapshot sirasinda degisiyorsa tam gorunum doner ve
        arada eklenen is bir sonraki deltada yakalanir (is kacmaz)."""
        qm = self._queue()
        self._add(qm)
        holder = qm.jobs[0]
        real = holder.snapshot

        def churn(windows=None, copy=True):
            snap = real(windows, copy)
            qm.add(JobSpec(url="http://127.0.0.1:1/churn.bin"), paused=True)
            return snap

        holder.snapshot = churn
        try:
            st = qm.stats()
        finally:
            holder.snapshot = real
        self.assertTrue(st["full"], "tutarli gorunum yok: tam gorunum gerekli")
        rev = st["rev"]
        late = self._add(qm, "http://127.0.0.1:1/sonra.bin")
        delta = qm.stats(since=rev)
        self.assertFalse(delta["full"])
        self.assertIn(late.id, [j["id"] for j in delta["jobs"]])


class EphemeralScrubTests(QueueCase):
    def test_ephemeral_record_redacts_mirror_and_merkle_urls(self):
        """L6: ayna ve merkle URL'lerindeki kimlik bilgisi diske yazilmaz."""
        qm = self._queue()
        spec = JobSpec(
            url="http://kullanici:sifre@127.0.0.1:1/dosya.bin",
            mirrors=["ftp://ayna:gizli@127.0.0.1:1/yedek.bin",
                     "https://127.0.0.1:1/temiz.bin"],
            merkle=("https://kullanici:sifre@127.0.0.1:1/"
                    "dosya.bin.ferdim-merkle"),
            ephemeral=True)
        job = qm.add(spec, paused=True)
        qm.flush()
        raw = (self.env.state_dir / QUEUE_FILE).read_text("utf-8")
        for secret in ("sifre", "gizli"):
            self.assertNotIn(secret, raw, "ozel pencere sirri diskte")
        rec = json.loads(raw)[0]
        self.assertEqual(rec["mirrors"][0], "ftp://127.0.0.1:1/yedek.bin")
        self.assertEqual(rec["mirrors"][1], "https://127.0.0.1:1/temiz.bin")
        self.assertEqual(rec["merkle"],
                         "https://127.0.0.1:1/dosya.bin.ferdim-merkle")
        # bellekteki is etkilenmez (indirme kimlik bilgisiyle surer)
        self.assertIn("ayna:gizli@", job.spec.mirrors[0])


class TickCostTests(QueueCase):
    """Bosta tick: yalniz degisen/calisan islerle ugrasilir."""

    def test_tick_only_touches_changed_or_running_jobs(self):
        qm = self._queue()
        jobs = [self._add(qm, f"http://127.0.0.1:1/t{i}.bin")
                for i in range(4)]
        jobs[0].state = JobState.DONE
        speeds, sampled = [], []
        real_speed = type(jobs[0]).update_speed
        real_observe = qm.metrics.observe

        def counting_speed(self_):
            speeds.append(self_.id)
            return real_speed(self_)

        def counting_observe(hot, coexist_active=False, states=None):
            sampled.append([j.id for j in hot])
            return real_observe(hot, coexist_active, states)

        jobtype = type(jobs[0])
        with mock.patch.object(jobtype, "update_speed", counting_speed), \
                mock.patch.object(qm.metrics, "observe", counting_observe):
            qm._tick()                 # ilk gecis: tabanlar kurulur
            speeds.clear()
            sampled.clear()
            qm._tick()                 # degisiklik yok: is basina is yok
            self.assertEqual(speeds, [], "duraklatilmis iste hiz hesaplandi")
            self.assertEqual(sampled, [[]], "degismeyen is yeniden orneklendi")
            # kosan is: her tick hiz alir ve orneklenir
            jobs[1].state = JobState.DOWNLOADING
            qm._tick()
            self.assertEqual(speeds, [jobs[1].id])
            self.assertEqual(sampled[-1], [jobs[1].id])
            # kosu bitince hiz bir kez sifirlanir
            jobs[1].speed = 1234.0
            jobs[1].state = JobState.DONE
            qm._tick()
            self.assertEqual(jobs[1].speed, 0.0, "biten iste hiz sifirlanmali")
            speeds.clear()
            qm._tick()
            self.assertEqual(speeds, [], "biten is artik hiz hesaplamaz")

    def test_tick_idle_flag_and_state_counts(self):
        qm = self._queue()
        job = self._add(qm)
        qm._tick()
        self.assertTrue(qm._idle, "is yokken tick uzun uyumali")
        self.assertEqual(qm._state_counts.get(JobState.PAUSED), 1)
        queued = qm.add(JobSpec(url="http://127.0.0.1:1/q.bin"))
        qm._tick()
        self.assertFalse(qm._idle, "kuyrukta is varsa kisa aralik")
        self.assertEqual(qm._state_counts.get(JobState.QUEUED), 1)
        qm.remove(job.id)
        qm._tick()                     # sayac temizligi tick'te
        self.assertIsNone(qm._state_counts.get(JobState.PAUSED))
        self.assertNotIn(job.id, qm._job_seen)
        self.assertEqual(qm._state_counts.get(JobState.QUEUED), 1)
        self.assertTrue(queued)


class ControlTests(QueueCase):
    def test_http_control_returns_at_once_while_the_job_is_slow(self):
        qm = self._queue()
        job = self._add(qm)
        ctx = self._ctx(qm, transport="http")
        entered, release, done = (threading.Event(), threading.Event(),
                                  threading.Event())

        def slow_resume():
            entered.set()
            release.wait(5)
            done.set()

        with mock.patch.object(job, "resume", slow_resume):
            t0 = time.monotonic()
            r = api.handle(ctx, "resume", {"id": job.id})
            elapsed = time.monotonic() - t0
            self.assertTrue(r.get("ok"), r)
            self.assertLess(elapsed, 0.5, "istek yavas islemi bekledi")
            self.assertTrue(entered.wait(5), "is kuyruga alinmadi")
            self.assertFalse(done.is_set(), "islem arka planda suruyor olmali")
        release.set()
        self.assertTrue(done.wait(5))

    def test_control_commands_keep_their_order_per_job(self):
        qm = self._queue()
        job = self._add(qm)
        calls: list[str] = []
        resumed = threading.Event()

        def pause():
            calls.append("pause")

        def resume():
            calls.append("resume")
            resumed.set()

        with mock.patch.object(job, "pause", pause), \
                mock.patch.object(job, "resume", resume):
            self.assertTrue(qm.control_async(job.id, "pause"))
            self.assertTrue(qm.control_async(job.id, "resume"))
            self.assertTrue(resumed.wait(5),
                            "denetim iscisi komutlari islemedi")
        self.assertEqual(calls, ["pause", "resume"])

    def test_socket_control_stays_synchronous(self):
        """CLI/GTK komut dondugunde durumu okur (cli_smoke): eski davranis."""
        qm = self._queue()
        job = self._add(qm)
        calls: list[str] = []
        with mock.patch.object(job, "resume", lambda: calls.append("resume")):
            r = api.handle(self._ctx(qm), "resume", {"id": job.id})
        self.assertTrue(r.get("ok"))
        self.assertEqual(calls, ["resume"], "soket yolu beklemeden dondu")

    def test_unknown_job_and_post_shutdown_are_rejected(self):
        qm = self._queue()
        ctx = self._ctx(qm, transport="http")
        self.assertFalse(api.handle(ctx, "pause", {"id": "yok"})["ok"])
        self.assertFalse(api.handle(ctx, "remove", {"id": "yok"})["ok"])
        qm.shutdown()
        self.assertFalse(qm.remove_async("all"))
        self.assertFalse(qm._ctl.is_alive(), "denetim iscisi kapanmadi")

    def test_http_remove_happens_in_the_background(self):
        qm = self._queue()
        job = self._add(qm)
        r = api.handle(self._ctx(qm, transport="http"), "remove",
                       {"id": job.id})
        self.assertTrue(r.get("ok"))
        self.assertTrue(wait_until(lambda: qm.get(job.id) is None, 5),
                        "arka plan silmesi tamamlanmadi")
        qm.flush()
        self.assertEqual(self._raw_queue(), [])


    def test_delete_files_keeps_a_new_jobs_file(self):
        """L3: ayni yola yazan canli is varsa son dosya SILINMEZ."""
        qm = self._queue()
        old = self._add(qm, URL)
        path = old.final_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"eski indirme")
        again = self._add(qm, URL)          # ayni URL yeniden eklendi
        self.assertEqual(again.final_path, path)
        self.assertTrue(qm.remove(old.id, delete_files=True))
        self.assertTrue(path.exists(), "yeni isin dosyasi silindi")
        # canli is kalmadiysa dosya silinir
        self.assertTrue(qm.remove(again.id, delete_files=True))
        self.assertFalse(path.exists())


class JobIndexTests(QueueCase):
    def test_get_and_remove_keep_the_index_consistent(self):
        qm = self._queue()
        a = self._add(qm)
        b = self._add(qm, "http://127.0.0.1:1/b.bin")
        self.assertIs(qm.get(a.id), a)
        self.assertEqual(len(qm._job_index), 2)
        self.assertTrue(qm.remove(a.id))
        self.assertIsNone(qm.get(a.id))
        self.assertNotIn(a.id, qm._job_index)
        self.assertIs(qm.get(b.id), b)
        # dogrudan listeye eklenen is (testler/olcum): indeks onarilir
        stray = DownloadJob(qm.env, qm.cfg,
                            JobSpec(url="http://127.0.0.1:1/s.bin"))
        with qm._lock:
            qm.jobs.append(stray)
        self.assertIs(qm.get(stray.id), stray)
        # kaldirilan isin izleri tick taramasinda temizlenir
        with qm._lock:
            qm.jobs.remove(stray)
        qm._tick()
        self.assertNotIn(stray.id, qm._job_index)


class ProberTests(QueueCase):
    def test_at_most_three_parallel_probes_and_removed_ids_are_forgotten(self):
        concurrency = {"now": 0, "peak": 0}
        lock = threading.Lock()
        release = threading.Event()
        entered = threading.Semaphore(0)
        calls: list[str] = []

        def fake_probe(self_, job):
            with lock:
                concurrency["now"] += 1
                concurrency["peak"] = max(concurrency["peak"],
                                          concurrency["now"])
                calls.append(job.id)
            entered.release()
            release.wait(5)
            with lock:
                concurrency["now"] -= 1

        with mock.patch.object(QueueManager, "_probe_job", fake_probe):
            qm = self._queue(prober=True)
            jobs = [qm.add(JobSpec(url=f"http://127.0.0.1:1/p{i}.bin"))
                    for i in range(5)]
            # ucu AYNI ANDA yoklamada olmali
            for _ in range(3):
                self.assertTrue(entered.acquire(timeout=5),
                                "yoklama baslamadi")
            with lock:
                self.assertEqual(concurrency["now"], 3,
                                 "yoklama es zamanliligi sinirli kalmali")
            release.set()
            self.assertTrue(wait_until(
                lambda: len(qm._size_probed) == 5, 5),
                f"tum isler yoklanmadi: {sorted(qm._size_probed)}")
            self.assertEqual(len(calls), 5, "her is bir kez yoklanir")
            with lock:
                self.assertLessEqual(concurrency["peak"], 3,
                                     "en fazla 3 es zamanli yoklama")

            # kaldirilan isin kimligi yoklama kumelerinde kalmaz
            gone = jobs[0]
            qm.remove(gone.id)
            self.assertNotIn(gone.id, qm._size_probed)
            self.assertNotIn(gone.id, qm._probing)


    def test_probe_claims_do_not_scan_the_queue(self):
        """P3: 10 000 KUYRUK isinde claim listeyi taramaz (O(1) popleft)."""
        qm = self._queue()
        with qm.bulk_save():
            for i in range(10000):
                qm.add(JobSpec(url=f"http://127.0.0.1:1/q{i}.bin"))
        self.assertEqual(len(qm._probe_q), 10000, qm._probe_q)
        self.assertEqual(len(qm._probe_set), 10000)
        calls = []
        real = QueueManager._probe_ready

        def counting(self_, job):
            calls.append(job.id)
            return real(self_, job)

        with mock.patch.object(QueueManager, "_probe_ready", counting):
            t0 = time.perf_counter()
            claimed = [qm._probe_claim() for _ in range(200)]
            elapsed = time.perf_counter() - t0
        self.assertEqual(len([j for j in claimed if j is not None]), 200)
        self.assertEqual(len(calls), 200, "claim is listesini taradi")
        self.assertLess(elapsed, 0.05, f"200 claim {elapsed * 1000:.0f} ms")
        self.assertEqual(len(qm._probe_q), 9800)
        self.assertEqual(len(qm._probing), 200)


class DispatchTests(QueueCase):
    def test_dispatch_picks_with_nsmallest_and_counts_slots(self):
        """P6: hazir islerin tamami siralanmaz; yuva sayimi artiklardan."""
        qm = self._queue(max_concurrent=1, smart_queue=True)
        jobs = [self._add(qm, f"http://127.0.0.1:1/s{i}.bin", paused=False)
                for i in range(20)]
        for i, job in enumerate(jobs):
            job.total = (i + 1) * 1000
            with qm._lock:
                qm._size_probed.add(job.id)
        started = []
        with mock.patch.object(qm, "_start_async",
                               lambda job: started.append(job) or True), \
                mock.patch.object(queue_mod.heapq, "nsmallest",
                                  wraps=queue_mod.heapq.nsmallest) as spy:
            qm._tick()
        self.assertEqual(spy.call_count, 1, "tek nsmallest cagrisi beklenir")
        self.assertEqual([j.id for j in started], [jobs[0].id],
                         "tek yuvaya en kucuk is secilmeli")
        # yuva dolu: durum sayaci (artimli) yeni is baslatmaz
        jobs[0].state = JobState.DOWNLOADING
        started.clear()
        qm._tick()
        self.assertEqual(started, [], "calisan is varken yeni is baslamaz")
        # _start_async de sayaci kullanir (is listesi taramaz)
        self.assertFalse(qm._start_async(jobs[5]))
        self.assertEqual(qm._started, {})


class LocalZoneCacheTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(util._LOCAL_ZONE)
        util._LOCAL_ZONE.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        util._LOCAL_ZONE.clear()
        util._LOCAL_ZONE.update(self._saved)

    @unittest.skipUnless(util._zone_from_key("Europe/Berlin") is not None,
                         "zoneinfo/Europe/Berlin yok")
    def test_zone_cache_is_reused_and_invalidated_on_tz_change(self):
        berlin = util._zone_from_key("Europe/Berlin")
        original = util._resolve_local_zone
        calls = []

        def counting(raw_tz):
            calls.append(raw_tz)
            return original(raw_tz)

        with mock.patch.dict(os.environ, {"TZ": "Europe/Berlin"}):
            with mock.patch.object(util, "_resolve_local_zone", counting):
                first = util.local_zone()
                second = util.local_zone()
            self.assertEqual(len(calls), 1, "ayni TZ icin yeniden cozuldu")
            self.assertIs(first, second)
            self.assertEqual(getattr(first, "key", ""), "Europe/Berlin")

            # TZ degisti: onbellek gecersiz, yeniden cozulur
            with mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo"}):
                with mock.patch.object(util, "_resolve_local_zone", counting):
                    tokyo = util.local_zone()
            self.assertEqual(len(calls), 2)
            self.assertEqual(getattr(tokyo, "key", ""), "Asia/Tokyo")
            self.assertIsNot(first, tokyo)

    def test_invalid_tz_falls_back_without_caching_the_offset(self):
        """POSIX kurali/gecersiz TZ: sabit ofset onbelleklenmez (DST)."""
        with mock.patch.dict(os.environ, {"TZ": "BOZUK/TZ"}):
            first = util.local_zone()
            self.assertEqual(util._LOCAL_ZONE.get("zone"), None)
            second = util.local_zone()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)


if __name__ == "__main__":
    unittest.main()
