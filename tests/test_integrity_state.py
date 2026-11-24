"""ROADMAP 1.11 - durum butunlugu ve cokmeye dayaniklilik.

Kapsam:
- atomik yazicilarin fsync'i (dosya + dizin): util.atomic_write_json,
  AppEnv.save_config, cdc.write_atomic.
- queue.json kurtarma: kayit kayit dogrulama (bozuk kayit atlanir),
  bozuk dosyanin zaman damgali .bad kopyasi, son iyi kopya (.prev) ve
  arayuze 'state_recovered' uyarisi.
- config.json / queue.json / hosts.json sema surumleri + sirali goc;
  bilinmeyen anahtarlarin korunmasi; bizden yeni surumun ezilmemesi.
- daemon tek ornek kilidi (flock) ve hosts.json es zamanli birlesme.

Testler yoklamali bekleme kullanmaz; her sey gecici koklerde kosar.
"""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import stat
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ferdim.api as api
import ferdim.paths as paths
import ferdim.profiles as profiles
from ferdim import cdc, daemon, util
from ferdim.engine.job import JobSpec, JobState
from ferdim.engine.queue import (QUEUE_FILE, QUEUE_PREV, QUEUE_ITEM_VERSION,
                                 QueueManager)
from ferdim.paths import AppEnv
from ferdim.profiles import FILE_NAME, HostProfiles
from tests.helpers import tmp_root


def quiet_queue(case, env, cfg: dict) -> QueueManager:
    """Arka plan donguleri kapali kuyruk: elle denetlenen durum dosyasi.

    Kapanma (_running=False) yerine yalniz donguler devre disi birakilir;
    tick/prober testte beklenmedik yazim yapmaz.
    """
    with mock.patch.object(QueueManager, "_tick_loop", lambda self: None), \
            mock.patch.object(QueueManager, "_prober_loop", lambda self: None):
        qm = QueueManager(env, cfg)
    case.addCleanup(qm.shutdown)
    return qm


class FsyncTests(unittest.TestCase):
    """Atomik yazimlar dosyayi ve dizini fsync eder (guc kesintisi)."""

    def test_atomic_write_json_fsyncs_file_and_directory(self):
        path = tmp_root(self, "ferdim-fsync-") / "durum.json"
        with mock.patch("os.fsync") as fs:
            util.atomic_write_json(path, {"a": 1})
        self.assertEqual(json.loads(path.read_text("utf-8")), {"a": 1})
        # en az iki cagri: dosya icerigi + dizin kaydi
        self.assertGreaterEqual(fs.call_count, 2)

    def test_save_config_fsyncs_file_and_directory(self):
        env = AppEnv(root=tmp_root(self, "ferdim-fsync-cfg-"))
        cfg = env.load_config()
        with mock.patch("os.fsync") as fs:
            env.save_config(cfg)
        self.assertGreaterEqual(fs.call_count, 2)
        self.assertIn("\n  ", env.config_path.read_text("utf-8"))

    def test_cdc_write_atomic_fsyncs_directory(self):
        path = tmp_root(self, "ferdim-fsync-cdc-") / "bildirim"
        with mock.patch("os.fsync") as fs:
            cdc.write_atomic(path, "govde\n")
        self.assertEqual(path.read_text("utf-8"), "govde\n")
        self.assertGreaterEqual(fs.call_count, 2)


class AtomicWriterTests(unittest.TestCase):
    """Yazicilar sabit '.tmp' adi kullanmaz: es zamanli yazar yarisi yok.

    Eski kod sabit `<ad>.tmp` dosyasini EEXIST'te siliyordu; daemon + CLI
    ayni anda yazinca biri digerinin canli gecici dosyasini siliyor ve bos
    dosya yayinlanabiliyordu.
    """

    def test_fixed_dot_tmp_file_is_never_touched(self):
        root = tmp_root(self, "ferdim-writer-")
        target = root / "queue.json"
        stale = target.with_suffix(target.suffix + ".tmp")
        stale.write_text("baska surecin canli gecici dosyasi", "utf-8")
        util.atomic_write_json(target, {"a": 1})
        # eski sabit ad silinmez: sahibi baska bir surec olabilir
        self.assertTrue(stale.exists())
        self.assertEqual(stale.read_text("utf-8"),
                         "baska surecin canli gecici dosyasi")
        self.assertEqual(json.loads(target.read_text("utf-8")), {"a": 1})

    def test_atomic_write_bytes_uses_a_unique_temp_in_target_dir(self):
        root = tmp_root(self, "ferdim-uniq-")
        target = root / "durum.json"
        real = util.tempfile.mkstemp
        seen = {}

        def spy(*args, **kw):
            seen.update(kw)
            return real(*args, **kw)

        with mock.patch.object(util.tempfile, "mkstemp", spy):
            util.atomic_write_bytes(target, b"veri", mode=0o600)
        self.assertEqual(seen.get("dir"), str(root))
        self.assertTrue(seen.get("prefix", "").startswith("durum.json"))
        self.assertEqual([p for p in root.iterdir() if p != target], [])

    def test_atomic_write_bytes_keeps_the_configured_mode(self):
        root = tmp_root(self, "ferdim-mode-")
        shared = root / "paylasilan"
        util.atomic_write_bytes(shared, b"x", mode=0o644)
        self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o644)
        private = root / "ozel.json"
        util.atomic_write_json(private, {})
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)

    def test_atomic_write_bytes_removes_temp_on_failure(self):
        root = tmp_root(self, "ferdim-fail-")
        target = root / "hedef"
        with mock.patch.object(util.os, "replace",
                               side_effect=OSError("disk hatasi")), \
                self.assertRaises(OSError):
            util.atomic_write_bytes(target, b"x")
        self.assertFalse(target.exists())
        self.assertEqual(list(root.iterdir()), [],
                         "basarisiz yazim gecici dosya birakmamali")

    def test_concurrent_writers_publish_a_complete_file(self):
        """Iki yazar ayni dosyaya: sonuc her zaman TAM bir govde olmali."""
        root = tmp_root(self, "ferdim-race-")
        target = root / "queue.json"
        payloads = [{"n": i, "pad": "x" * (20_000 + i)} for i in range(2)]
        errors: list = []

        def writer(obj):
            try:
                for _ in range(15):
                    util.atomic_write_json(target, obj)
            except Exception as exc:        # eski kodda FileNotFoundError
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(p,))
                   for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertIn(json.loads(target.read_text("utf-8")), payloads)
        self.assertEqual(list(root.glob("*.tmp")), [],
                         "gecici dosya kalmamali")

    def test_concurrent_save_config_publishes_complete_json(self):
        env = AppEnv(root=tmp_root(self, "ferdim-race-cfg-"))
        cfgs = []
        for seg in (4, 8):
            cfg = env.load_config()
            cfg["segments"] = seg
            cfg["pad"] = "x" * (20_000 + seg)
            cfgs.append(cfg)
        errors: list = []

        def writer(cfg):
            try:
                for _ in range(10):
                    env.save_config(cfg)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(c,)) for c in cfgs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertIn(json.loads(env.config_path.read_text("utf-8")), cfgs)


class QueueRecoveryTests(unittest.TestCase):
    """Bozuk queue.json cokme dongusu ya da bos kuyruk yaratmaz."""

    def setUp(self):
        self.env = AppEnv(root=tmp_root(self, "ferdim-state-q-"))
        self.cfg = self.env.load_config()
        self.cfg.update({"turbo_mode": False, "max_concurrent": 1,
                         "smart_queue": False, "retries": 0, "timeout": 2})
        self.path = self.env.state_dir / QUEUE_FILE
        self.prev = self.env.state_dir / QUEUE_PREV

    def _queue(self) -> QueueManager:
        return quiet_queue(self, self.env, self.cfg)

    def _add(self, qm: QueueManager, name: str):
        return qm.add(JobSpec(url=f"http://127.0.0.1:1/{name}"), paused=True)

    def test_01_corrupt_file_is_quarantined_and_prev_restored(self):
        qm = self._queue()
        a = self._add(qm, "a.bin")
        b = self._add(qm, "b.bin")
        qm.shutdown()                       # ikinci yazim .prev'i olusturur
        self.assertTrue(self.path.exists())
        self.assertTrue(self.prev.exists(), "son iyi kopya yazilmaliydi")

        self.path.write_text("{bozuk", "utf-8")
        qm2 = self._queue()

        self.assertEqual(qm2.stats()["warning"], "state_recovered")
        self.assertEqual({j.id for j in qm2.jobs}, {a.id, b.id},
                         "son iyi kopyadan kuyruk geri gelmeli")
        # bozuk dosya zaman damgali adla saklanir, uzerine yazilmaz
        bad = sorted(self.env.state_dir.glob(QUEUE_FILE + ".bad-*"))
        self.assertEqual(len(bad), 1, bad)
        self.assertEqual(bad[0].read_text("utf-8"), "{bozuk")
        # onarilmis ana dosya yeniden yazilir (duz liste sozlesmesi korunur)
        self.assertIsInstance(json.loads(self.path.read_text("utf-8")), list)

        # API/arayuz yolu da uyariyi gorur
        r = api.handle({"qm": qm2, "env": self.env, "cfg": self.cfg},
                       "stats", {})
        self.assertEqual(r["warning"], "state_recovered")

    def test_02_one_bad_item_is_skipped_not_the_whole_queue(self):
        self.path.write_text(json.dumps([
            {"id": "iyi", "url": "http://127.0.0.1:1/iyi.bin",
             "state": "paused"},
            {"url": 42},                                  # url turu bozuk
            {"id": "bozuk", "url": "http://127.0.0.1:1/x.bin",
             "headers": ["K: V"]},                        # baslik turu bozuk
        ]), "utf-8")
        qm = self._queue()

        self.assertEqual([j.id for j in qm.jobs], ["iyi"])
        self.assertEqual(qm.stats()["warning"], "state_recovered")
        # elenen kayitlar ana dosyaya geri yazilmaz
        saved = json.loads(self.path.read_text("utf-8"))
        self.assertEqual([rec["id"] for rec in saved], ["iyi"])

    def test_03_unknown_state_degrades_and_unknown_keys_survive(self):
        self.path.write_text(json.dumps([{
            "id": "gelecek",
            "url": "http://127.0.0.1:1/gelecek.bin",
            "state": "gelecek-durum",                     # bilinmeyen durum
            "future_field": {"a": [1, 2]},                # bilinmeyen anahtar
        }]), "utf-8")
        qm = self._queue()
        job = qm.get("gelecek")
        self.assertIsNotNone(job)
        self.assertEqual(job.state, JobState.QUEUED)
        qm._save()

        rec = json.loads(self.path.read_text("utf-8"))[0]
        self.assertEqual(rec["v"], QUEUE_ITEM_VERSION)
        self.assertEqual(rec["future_field"], {"a": [1, 2]},
                         "bilinmeyen alan yazimda korunmali")

    def test_04_clean_queue_reports_no_warning(self):
        qm = self._queue()
        self._add(qm, "temiz.bin")
        qm.shutdown()
        qm2 = self._queue()
        self.assertEqual(qm2.stats()["warning"], "")
        self.assertEqual(len(qm2.jobs), 1)

    def test_05_newer_record_version_is_not_downgraded(self):
        self.path.write_text(json.dumps([{
            "v": 7, "id": "yeni", "url": "http://127.0.0.1:1/yeni.bin",
            "state": "paused", "future_field": "x",
        }]), "utf-8")
        qm = self._queue()
        self.assertEqual(qm.get("yeni").state, JobState.PAUSED)
        qm._save()
        rec = json.loads(self.path.read_text("utf-8"))[0]
        self.assertEqual(rec["v"], 7, "daha yeni surum numarasi korunmali")
        self.assertEqual(rec["future_field"], "x")

    def test_06_error_reason_survives_restart(self):
        """Hata nedeni kuyrukta kalir; durum yine 'paused' olarak doner."""
        qm = self._queue()
        job = qm.add(JobSpec(url="http://127.0.0.1:1/hata.bin"), paused=True)
        job.state = JobState.ERROR
        job.error = "sunucu 403 dondurdu"
        job.error_code = "http_403"
        qm._save()

        rec = json.loads(self.path.read_text("utf-8"))[0]
        self.assertEqual(rec["error"], "sunucu 403 dondurdu")
        self.assertEqual(rec["error_code"], "http_403")

        qm.shutdown()
        again = self._queue().get(job.id)
        self.assertIsNotNone(again)
        # mevcut davranis: hatali is kendiliginden yeniden denenmez
        self.assertEqual(again.state, JobState.PAUSED)
        snap = again.snapshot()
        self.assertEqual(snap["error"], "sunucu 403 dondurdu")
        self.assertEqual(snap["error_code"], "http_403")

    def test_07_quarantine_keeps_only_the_newest_three_copies(self):
        for stamp in ("20200101-000000", "20200102-000000",
                      "20200103-000000", "20200104-000000"):
            (self.env.state_dir / f"{QUEUE_FILE}.bad-{stamp}").write_text(
                "x", "utf-8")
        self.path.write_text("{bozuk", "utf-8")
        self._queue()

        bads = sorted(p.name for p in
                      self.env.state_dir.glob(QUEUE_FILE + ".bad-*"))
        self.assertEqual(len(bads), 3, bads)
        self.assertNotIn(f"{QUEUE_FILE}.bad-20200101-000000", bads,
                         "en eski kopyalar silinmeli")

    def test_08_clear_finished_drops_stopped_job_temp_files(self):
        qm = self._queue()
        job = qm.add(JobSpec(url="http://127.0.0.1:1/yarim.bin"), paused=True)
        job.state = JobState.ERROR
        part = job.part_path
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"yarim veri")
        meta = job.meta_path
        meta.write_text("{}", "utf-8")
        segs = Path(str(meta) + ".segs")
        segs.mkdir()
        (segs / "0.part").write_bytes(b"parca")

        qm.clear_finished()

        self.assertIsNone(qm.get(job.id))
        self.assertFalse(part.exists(), "yarim .part sahipsiz kalmamali")
        self.assertFalse(meta.exists(), "denetim noktasi silinmeli")
        self.assertFalse(segs.exists(), ".segs klasoru silinmeli")

    def test_09_clear_finished_keeps_a_downloaded_file(self):
        qm = self._queue()
        job = qm.add(JobSpec(url="http://127.0.0.1:1/biten.bin"), paused=True)
        final = job.final_path
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"tam veri")
        job.state = JobState.DONE

        qm.clear_finished()

        self.assertIsNone(qm.get(job.id))
        self.assertTrue(final.exists(), "indirilen dosya silinmemeli")

    def test_10_ephemeral_error_text_does_not_leak_credentials(self):
        secret = "PAROLA-SIRRI-999"
        qm = self._queue()
        job = qm.add(JobSpec(url=f"http://kullanici:{secret}@127.0.0.1:1/a.bin",
                             password=secret, ephemeral=True), paused=True)
        job.state = JobState.ERROR
        job.error = f"istek basarisiz: http://kullanici:{secret}@127.0.0.1:1/a.bin"
        job.error_code = "http_403"
        qm._save()

        raw = self.path.read_text("utf-8")
        self.assertNotIn(secret, raw, "ozel pencere hatasi kimlik sizdirmamali")
        self.assertIn("http_403", raw)
        # bellekteki is etkilenmez: kullanici nedeni gormeye devam eder
        self.assertIn(secret, job.error)


class ConfigSchemaTests(unittest.TestCase):
    """config.json surum gocu; bilinmeyen anahtarlar ve yeni surum korunur."""

    def test_v0_config_migrates_and_preserves_unknown_keys(self):
        env = AppEnv(root=tmp_root(self, "ferdim-state-cfg-"))
        env.config_path.write_text(json.dumps(
            {"segments": 4, "gelecek": {"x": 1}}), "utf-8")
        cfg = env.load_config()
        self.assertEqual(cfg["version"], paths.CONFIG_VERSION)
        self.assertEqual(cfg["segments"], 4)
        self.assertEqual(cfg["gelecek"], {"x": 1})

        env.save_config(cfg)
        raw = json.loads(env.config_path.read_text("utf-8"))
        self.assertEqual(raw["version"], paths.CONFIG_VERSION)
        self.assertEqual(raw["gelecek"], {"x": 1})

    def test_newer_config_version_is_loaded_and_not_destroyed(self):
        env = AppEnv(root=tmp_root(self, "ferdim-state-cfg2-"))
        env.config_path.write_text(json.dumps(
            {"version": 99, "segments": 3, "gelecek": True}), "utf-8")
        cfg = env.load_config()
        self.assertEqual(cfg["version"], 99)
        self.assertTrue(cfg["gelecek"])

        env.save_config(cfg)
        raw = json.loads(env.config_path.read_text("utf-8"))
        self.assertEqual(raw["version"], 99, "yeni surum numarasi ezilmemeli")
        self.assertTrue(raw["gelecek"])

    def test_migrations_run_in_ascending_order(self):
        seen = []

        def first(raw):
            seen.append(1)

        def second(raw):
            seen.append(2)

        with mock.patch.object(paths, "CONFIG_VERSION", 2), \
                mock.patch.dict(paths.CONFIG_MIGRATIONS,
                                {1: first, 2: second}):
            cfg = paths._migrate_config({"segments": 4})
        self.assertEqual(seen, [1, 2])
        self.assertEqual(cfg["version"], 2)

    def test_config_set_round_trip_keeps_unknown_keys(self):
        env = AppEnv(root=tmp_root(self, "ferdim-state-cfg3-"))
        env.config_path.write_text(json.dumps(
            {"gelecek": "korunur", "segments": 4}), "utf-8")
        cfg = env.load_config()
        ctx = {"qm": None, "env": env, "cfg": cfg,
               "set_cfg": env.save_config}
        r = api.handle(ctx, "config-set", {"config": {"segments": 6}})
        self.assertTrue(r.get("ok"), r)
        raw = json.loads(env.config_path.read_text("utf-8"))
        self.assertEqual(raw["segments"], 6)
        self.assertEqual(raw["gelecek"], "korunur")


class HostsSchemaTests(unittest.TestCase):
    """hosts.json sema gocu, bilinmeyen anahtarlar ve es zamanli birlesme."""

    def setUp(self):
        self.env = AppEnv(root=tmp_root(self, "ferdim-state-hosts-"))
        self.path = self.env.state_dir / FILE_NAME

    def _write(self, raw: dict) -> None:
        self.path.write_text(json.dumps(raw), "utf-8")

    def _saved(self) -> dict:
        return json.loads(self.path.read_text("utf-8"))

    def test_unknown_keys_survive_a_write(self):
        now = time.time()
        self._write({"version": 1, "note": "gelecek alan",
                     "hosts": {"a.test": {"links": 4, "last_seen": now,
                                          "future": [1]}}})
        store = HostProfiles(self.env)
        self.assertEqual(store.get("a.test")["future"], [1])

        self.assertTrue(store.record("b.test", links=2))
        saved = self._saved()
        self.assertEqual(saved["note"], "gelecek alan")
        self.assertEqual(saved["hosts"]["a.test"]["future"], [1])
        self.assertEqual(saved["version"], 1)

    def test_v0_file_is_migrated_to_current_version(self):
        self._write({"hosts": {"a.test": {"links": 3,
                                          "last_seen": time.time()}}})
        store = HostProfiles(self.env)
        self.assertEqual(store.get("a.test")["links"], 3)
        self.assertTrue(store.record("a.test", links=5))
        self.assertEqual(self._saved()["version"], 1)

    def test_newer_version_is_preserved(self):
        self._write({"version": 7, "note": 1, "hosts": {}})
        store = HostProfiles(self.env)
        self.assertTrue(store.record("a.test", links=2))
        saved = self._saved()
        self.assertEqual(saved["version"], 7, "yeni surum ezilmemeli")
        self.assertEqual(saved["note"], 1)

    def test_change_by_another_process_is_merged(self):
        """Bellekteki depo bayatlasa da baska surecin kaydi ezilmez."""
        daemon_store = HostProfiles(self.env)
        other_store = HostProfiles(self.env)
        self.assertEqual(other_store.hosts(), [])       # bos dosyayi onbellege al

        self.assertTrue(daemon_store.record("a.test", links=2))
        self.assertTrue(other_store.record("b.test", links=4))  # tazeler

        saved = self._saved()
        self.assertEqual(set(saved["hosts"]), {"a.test", "b.test"})

    def test_deletion_by_another_process_is_not_resurrected(self):
        daemon_store = HostProfiles(self.env)
        other_store = HostProfiles(self.env)
        self.assertTrue(daemon_store.record("a.test", links=2))
        self.assertEqual([h["host"] for h in other_store.hosts()], ["a.test"])

        self.assertTrue(daemon_store.forget("a.test"))
        self.assertTrue(other_store.record("b.test", links=4))

        saved = self._saved()
        self.assertEqual(set(saved["hosts"]), {"b.test"})

    def test_migration_receives_the_whole_file(self):
        """Gocler kayit sozlugu degil TUM govdeyi gorur (ust duzey goc)."""
        self._write({"hosts": {"eski.test": {"links": 3,
                                             "last_seen": time.time()}}})
        seen = {}

        def migrate(raw):
            seen["top"] = set(raw)
            hosts = raw.get("hosts") or {}
            # ust duzeyi yeniden duzenleyen bir goc: kaydi tasi
            raw["hosts"] = {"yeni.test": dict(next(iter(hosts.values())))}
            raw["note"] = "goc"

        with mock.patch.dict(profiles.HOSTS_MIGRATIONS,
                             {1: migrate, 2: lambda raw: None}), \
                mock.patch.object(profiles, "FORMAT_VERSION", 2):
            store = HostProfiles(self.env)
            hosts = store.hosts()

        self.assertIn("hosts", seen["top"], "goc tam dosya govdesini almali")
        self.assertEqual([h["host"] for h in hosts], ["yeni.test"])
        self.assertIsNone(store.get("eski.test"))

    def test_mutations_take_the_sidecar_flock(self):
        store = HostProfiles(self.env)
        real = profiles.fcntl.flock
        calls = []

        def spy(fd, op):
            calls.append(op)
            return real(fd, op)

        with mock.patch.object(profiles.fcntl, "flock", spy):
            self.assertTrue(store.record("a.test", links=2))
        self.assertEqual(calls, [profiles.fcntl.LOCK_EX])
        self.assertTrue((self.env.state_dir / (FILE_NAME + ".lock")).exists())

    def test_sidecar_lock_is_exclusive_across_writers(self):
        store = HostProfiles(self.env)
        store.record("a.test", links=2)
        lock_path = self.env.state_dir / (FILE_NAME + ".lock")
        held = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(held, fcntl.LOCK_EX)
            other = os.open(lock_path, os.O_RDWR)
            try:
                with self.assertRaises(OSError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(other)
        finally:
            os.close(held)


class InstanceLockTests(unittest.TestCase):
    """Tek daemon kilidi: bayat PID iki daemon baslatamaz."""

    def test_second_lock_fails_while_first_is_held(self):
        env = AppEnv(root=tmp_root(self, "ferdim-state-lock-"))
        first = daemon.instance_lock(env)
        self.assertIsInstance(first, int)
        try:
            self.assertIsNone(daemon.instance_lock(env),
                              "ikinci kilit alinmamaliydi")
        finally:
            os.close(first)

        again = daemon.instance_lock(env)
        self.assertIsNotNone(again, "kilit birakilinca yeniden alinmali")
        os.close(again)

    def test_stale_pid_file_does_not_block(self):
        env = AppEnv(root=tmp_root(self, "ferdim-state-lock2-"))
        env.pid_path.write_text("999999")               # olu PID
        fd = daemon.instance_lock(env)
        self.assertIsNotNone(fd)
        os.close(fd)

    def test_daemon_main_refuses_a_second_instance(self):
        env = AppEnv(root=tmp_root(self, "ferdim-state-lock3-"))
        env.pid_path.write_text("4242")
        # sanitize bu degeri 32'ye ceker; kilit ONCE alinirsa dosya dokunulmaz
        env.config_path.write_text(json.dumps({"segments": 999}), "utf-8")
        fd = daemon.instance_lock(env)
        self.addCleanup(os.close, fd)

        err = io.StringIO()
        with mock.patch.object(daemon, "AppEnv", return_value=env), \
                contextlib.redirect_stderr(err):
            rc = daemon.main()
        self.assertEqual(rc, 1)
        self.assertIn("zaten calisiyor", err.getvalue())
        # ikinci ornek, birincinin config'ini okuyup yeniden yazmadi
        self.assertEqual(json.loads(env.config_path.read_text("utf-8")),
                         {"segments": 999})

    def test_lock_file_lives_in_state_dir(self):
        env = AppEnv(root=tmp_root(self, "ferdim-state-lock4-"))
        fd = daemon.instance_lock(env)
        try:
            self.assertTrue(env.lock_path.exists())
            self.assertEqual(env.lock_path.parent, env.state_dir)
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
