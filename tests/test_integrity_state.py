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
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ferdim.api as api
import ferdim.paths as paths
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
        fd = daemon.instance_lock(env)
        self.addCleanup(os.close, fd)

        err = io.StringIO()
        with mock.patch.object(daemon, "AppEnv", return_value=env), \
                contextlib.redirect_stderr(err):
            rc = daemon.main()
        self.assertEqual(rc, 1)
        self.assertIn("zaten calisiyor", err.getvalue())

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
