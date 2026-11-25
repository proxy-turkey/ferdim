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
from ferdim.engine.job import JobSpec, JobState
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

    def _queue(self, prober=False, **over) -> QueueManager:
        self.env = AppEnv(root=tmp_root(self, "ferdim-p113-"))
        self.cfg = self.env.load_config()
        self.cfg.update({"max_concurrent": 0, "smart_queue": False,
                         "retries": 0, "timeout": 1, "schedule": {}})
        self.cfg.update(over)
        patches = [
            # yazici ve tick elle denetlenir (ag yok, sabit dis yazimi yok)
            mock.patch.object(QueueManager, "_tick_loop", lambda self: None),
            mock.patch.object(QueueManager, "_save_loop", lambda self: None),
        ]
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

    def test_single_add_is_durable_immediately(self):
        qm = self._queue()
        job = self._add(qm)
        self.assertEqual([r["id"] for r in self._raw_queue()], [job.id])

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

        def blocking_snapshot():
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

        def churn():
            snap = real()
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
