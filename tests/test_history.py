"""Kuyruk gecmisi: SQLite semasi/gocu, kayit, arama, istatistik, yeniden
indirme, saklama suresi (prune) ve kuyruk dayanikliligi."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import api, cli, history
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.engine.queue import QueueManager
from ferdim.i18n import LANGS, S, t
from ferdim.paths import DEFAULTS, AppEnv
from tests.helpers import TINY, start_server


def wait_state(job, states, timeout=25):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if job.state in states:
            return True
        time.sleep(0.1)
    return False


def make_job(env, cfg, jid, *, url="https://example.org/dosya.iso",
             filename="dosya.iso", state=JobState.DONE, size=1000, done=None,
             links=(2, 4), created_ago=12.0, error_code="", checksum="ok",
             active_ago=None, **spec_kw):
    spec = JobSpec(url=url, filename=filename, **spec_kw)
    job = DownloadJob(env, cfg, spec, job_id=jid)
    job.state = state
    job.total = size
    job.done = size if done is None else done
    job.created = time.time() - created_ago
    # aktif sure (gecmis 'duration'): varsayilan created_ago kadar
    job._active_secs = float(created_ago if active_ago is None else active_ago)
    job.error_code = error_code
    job.extra["checksum"] = checksum
    job._links_sum, job._links_peak = links
    job._links_samples = 5
    return job


def set_finished(hist, job_id, ts):
    conn = sqlite3.connect(str(hist.path))
    try:
        conn.execute("UPDATE history SET finished_at=? WHERE id=?", (ts, job_id))
        conn.commit()
    finally:
        conn.close()


class _BrokenHistory:
    """DB tamamen bozuk: kayit ve temizlik istisna firlatir."""

    def record(self, job):
        raise RuntimeError("db yok")

    def prune(self, days):
        raise RuntimeError("db yok")


class HistoryBase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ferdim-hist-"))
        self.env = AppEnv(root=self.root)
        self.cfg = dict(DEFAULTS)
        self.hist = history.open_history(self.env)

    def tearDown(self):
        for _ in range(3):
            try:
                import shutil
                shutil.rmtree(self.root, ignore_errors=True)
                return
            except OSError:
                time.sleep(0.05)


class SchemaTests(HistoryBase):
    def test_01_schema_version_and_mode(self):
        job = make_job(self.env, self.cfg, "j1")
        self.assertTrue(self.hist.record(job))
        self.assertEqual(self.hist.path.name, "history.db")
        self.assertEqual(self.hist.path.stat().st_mode & 0o777, 0o600,
                         "gecmis dosyasi 0600 olmali")
        conn = sqlite3.connect(str(self.hist.path))
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                             history.SCHEMA_VERSION)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0],
                             "wal")
        finally:
            conn.close()

    def test_02_migration_from_version_zero(self):
        path = self.env.state_dir / "history.db"
        path.write_bytes(b"")               # surum 0: bos dosya
        os.chmod(path, 0o644)               # gevsek mod duzeltilmeli
        h = history.History(path)
        job = make_job(self.env, self.cfg, "m1", url="https://a.example/f.iso")
        self.assertTrue(h.record(job), "goc sonrasi yazim calismali")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        conn = sqlite3.connect(str(path))
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            conn.close()
        found = h.search("f.iso")
        self.assertEqual(found["total"], 1)

    def test_03_repeat_terminal_updates_same_row(self):
        job = make_job(self.env, self.cfg, "r1", state=JobState.DONE)
        self.hist.record(job)
        job.state = JobState.ERROR
        job.done = 10
        job.error_code = "network"
        self.hist.record(job)
        found = self.hist.search("")
        self.assertEqual(found["total"], 1, "ayni is kimligi tek satir")
        self.assertEqual(found["items"][0]["state"], "error")
        self.assertEqual(found["items"][0]["error_code"], "network")


class RecordTests(HistoryBase):
    def test_04_record_fields_and_redaction(self):
        url = "ftp://kullanici:gizli@ftp.example.com/arsiv.zip"
        # 60 sn once olusturuldu ama yalniz 12 sn AKTIF kaldi: kuyrukta
        # bekleme/duraklama 'sure' alanina girmemeli
        job = make_job(self.env, self.cfg, "f1", url=url, filename="arsiv.zip",
                       state=JobState.CANCELED, size=500, done=120,
                       links=(3, 6), error_code="", created_ago=60.0,
                       active_ago=12.0)
        self.hist.record(job)
        found = self.hist.search("arsiv")
        row = found["items"][0]
        self.assertEqual(row["url"], "ftp://kullanici@ftp.example.com/arsiv.zip")
        self.assertNotIn("gizli", json.dumps(found, ensure_ascii=False),
                         "URL sifresi listelere sizmamali")
        self.assertNotIn("password", row, "opts listede donmez")
        self.assertEqual(row["host"], "ftp.example.com")
        self.assertEqual((row["size"], row["bytes"]), (500, 120))
        self.assertEqual(row["avg_links"], 0.6)
        self.assertEqual(row["peak_links"], 6)
        self.assertAlmostEqual(row["duration"], 12.0, delta=2)
        # opts (yalniz sunucu ici) sifreyi yeniden indirme icin tasir
        rec = self.hist.get("f1")
        self.assertEqual(rec["opts"].get("password"), "gizli")

    def test_04b_duration_counts_only_active_time(self):
        """'sure' = connecting/downloading saniyeleri; kuyrukta bekleme ve
        duraklama eklenmez (eski hesap bitis-olusturma idi)."""
        job = make_job(self.env, self.cfg, "d1", created_ago=100.0,
                       active_ago=0.0)
        job.state = JobState.CONNECTING
        job._active_mark -= 21.0          # 21 sn aktif kaldi
        job.state = JobState.PAUSED       # sayac burada durur
        self.assertAlmostEqual(job.active_seconds(), 21.0, delta=0.2)
        job.state = JobState.PAUSED       # duraklamada gecen sure eklenmez
        self.assertAlmostEqual(job.active_seconds(), 21.0, delta=0.2)
        job.state = JobState.DONE
        self.hist.record(job)
        row = self.hist.get("d1")
        self.assertAlmostEqual(row["duration"], 21.0, delta=0.3)

    def test_04c_finished_job_keeps_active_time(self):
        """Is bitince (done) sayac durur; sonradan gecen sure eklenmez."""
        job = make_job(self.env, self.cfg, "d2", created_ago=10.0,
                       active_ago=0.0)
        job.state = JobState.DOWNLOADING
        job._active_mark -= 3.0
        job.state = JobState.DONE
        got = job.active_seconds()
        self.assertAlmostEqual(got, 3.0, delta=0.2)
        self.assertAlmostEqual(job.active_seconds(), got, delta=0.05)

    def test_05_error_and_checksum_recorded(self):
        job = make_job(self.env, self.cfg, "e1", state=JobState.ERROR,
                       error_code="checksum_mismatch", checksum="mismatch",
                       size=800, done=800)
        self.hist.record(job)
        row = self.hist.get("e1")
        self.assertEqual(row["state"], "error")
        self.assertEqual(row["error_code"], "checksum_mismatch")
        self.assertEqual(row["checksum"], "mismatch")
        self.assertEqual(row["bytes"], 800)

    def test_06_empty_id_is_ignored(self):
        job = make_job(self.env, self.cfg, "tmp")
        job.id = ""
        self.assertFalse(self.hist.record(job))
        self.assertEqual(self.hist.search("")["total"], 0)


class SearchStatsTests(HistoryBase):
    def setUp(self):
        super().setUp()
        self.hist.record(make_job(
            self.env, self.cfg, "a1", url="https://mirror.example.org/iso/debian.iso",
            filename="debian.iso", size=1000, links=(2, 4), created_ago=10))
        self.hist.record(make_job(
            self.env, self.cfg, "b1", url="https://files.example.net/muzik.mp3",
            filename="muzik.mp3", state=JobState.ERROR, size=500, done=250,
            links=(4, 8), created_ago=20, error_code="network"))
        self.hist.record(make_job(
            self.env, self.cfg, "c1", url="ftp://ftp.example.com/arsiv.zip",
            filename="arsiv.zip", state=JobState.CANCELED, size=300, done=30,
            links=(1, 1), created_ago=30))
        now = time.time()
        for i, jid in enumerate(("a1", "b1", "c1")):
            set_finished(self.hist, jid, now - i)

    def test_07_search_text_state_and_paging(self):
        self.assertEqual(self.hist.search("debian")["total"], 1)
        self.assertEqual(self.hist.search("example.org")["items"][0]["id"], "a1")
        self.assertEqual(self.hist.search("")["total"], 3)
        self.assertEqual(self.hist.search("", state="error")["total"], 1)
        self.assertEqual(self.hist.search("", state="bogus")["total"], 3,
                         "gecersiz durum suzgeci yok sayilir")
        page = self.hist.search("", limit=1, offset=1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["items"][0]["id"], "b1")   # en yeni a1, sonra b1
        self.assertEqual(self.hist.search("%")["total"], 0,
                         "yuzde isareti joker degil, duz metin")

    def test_08_stats_totals_and_hosts(self):
        st = self.hist.stats()
        self.assertEqual(st["count"], 3)
        self.assertEqual(st["bytes"], 1000 + 250 + 30)
        hosts = {h["host"]: h for h in st["hosts"]}
        self.assertEqual(set(hosts), {"mirror.example.org", "files.example.net",
                                      "ftp.example.com"})
        self.assertEqual(hosts["mirror.example.org"]["avg_links"], 0.4)
        self.assertEqual(hosts["mirror.example.org"]["peak_links"], 4.0)
        self.assertGreater(hosts["mirror.example.org"]["avg_speed"], 0)

    def test_09_clear_and_older_than(self):
        removed = self.hist.clear(older_than_days=3650)
        self.assertEqual(removed, 0, "3.5 aydan eski kayit yok")
        self.assertEqual(self.hist.search("")["total"], 3)
        removed = self.hist.clear(older_than_days=0)   # simdi: hepsi eski
        self.assertEqual(removed, 3)
        self.assertEqual(self.hist.search("")["total"], 0)

    def test_10_prune_respects_days(self):
        old = make_job(self.env, self.cfg, "old1", url="https://old.example/x")
        self.hist.record(old)
        set_finished(self.hist, "old1", time.time() - 40 * 86400)
        self.assertEqual(self.hist.prune(0), 0, "0 = sonsuza kadar sakla")
        self.assertEqual(self.hist.search("")["total"], 4)
        self.assertEqual(self.hist.prune(30), 1)
        self.assertEqual(self.hist.search("")["total"], 3)


class FailureTests(HistoryBase):
    def test_11_db_failure_is_swallowed(self):
        broken_path = self.env.state_dir / "bozuk" / "history.db"
        broken_path.parent.mkdir(parents=True, exist_ok=True)
        broken_path.mkdir()                 # DB yerine dizin: acilamaz
        h = history.History(broken_path)
        job = make_job(self.env, self.cfg, "x1")
        self.assertFalse(h.record(job))
        self.assertIsNone(h.search(""))
        self.assertIsNone(h.stats())
        self.assertEqual(h.clear(), 0)
        self.assertEqual(h.prune(30), 0)

    def test_12_queue_survives_broken_history(self):
        qm = QueueManager(self.env, self.cfg)
        try:
            qm.history = _BrokenHistory()
            job = make_job(self.env, self.cfg, "q1")
            qm._job_changed(job)            # istisna sizmamali
            self.assertEqual(job.state, JobState.DONE)
        finally:
            qm.shutdown()


class QueueRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ferdim-hq-"))
        self.env = AppEnv(root=self.root)
        self.cfg = dict(DEFAULTS)
        self.cfg["retries"] = 0
        self.cfg["timeout"] = 5
        import shutil
        self.addCleanup(shutil.rmtree, self.root, True)
        self.qm = QueueManager(self.env, self.cfg)
        self.addCleanup(self.qm.shutdown)
        self.ctx = {"qm": self.qm, "env": self.env, "cfg": self.cfg,
                    "set_cfg": self.env.save_config}

    def test_13_done_job_is_recorded(self):
        r = api.handle(self.ctx, "add", {"url": self.base + "/tiny.bin"})
        self.assertTrue(r["ok"], r)
        job = self.qm.get(r["id"])
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)), job.state)
        self.assertEqual(job.state, JobState.DONE)
        row = self.qm.history.get(r["id"])
        self.assertIsNotNone(row, "biten is gecmise yazilmali")
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["filename"], "tiny.bin")
        self.assertEqual(row["bytes"], len(TINY))
        self.assertEqual(row["host"], "127.0.0.1")
        self.assertIn(row["checksum"], ("", "ok"))
        self.assertGreaterEqual(row["peak_links"], 0)
        self.assertGreater(row["duration"], 0, "aktif sure kaydedilmeli")
        self.assertNotIn("password", json.dumps(row, ensure_ascii=False))

    def test_14_error_job_is_recorded(self):
        r = api.handle(self.ctx, "add", {"url": self.base + "/yok.bin"})
        self.assertTrue(r["ok"], r)
        job = self.qm.get(r["id"])
        self.assertTrue(wait_state(job, (JobState.ERROR, JobState.DONE)), job.state)
        self.assertEqual(job.state, JobState.ERROR)
        self.assertEqual(job.error_code, "not_found")
        row = self.qm.history.get(r["id"])
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "error")
        self.assertEqual(row["error_code"], "not_found")
        self.assertEqual(row["bytes"], 0)

    def test_15_startup_prunes_old_history(self):
        self.qm.shutdown()                  # bu test kendi kuyrugunu kurar
        old = make_job(self.env, self.cfg, "old1")
        self.qm.history.record(old)
        set_finished(self.qm.history, "old1", time.time() - 10 * 86400)
        cfg = dict(self.cfg)
        cfg["history_days"] = 5
        qm2 = QueueManager(self.env, cfg)
        try:
            self.assertEqual(qm2.history.search("")["total"], 0,
                             "history_days eski kayitlari baslangicta siler")
        finally:
            qm2.shutdown()

    def test_16_api_history_commands(self):
        self.qm.history.record(make_job(
            self.env, self.cfg, "h1", url="https://ornek.example/a.iso",
            filename="a.iso", size=2048, links=(3, 6)))
        found = api.handle(self.ctx, "history", {"q": "a.iso"})
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["items"][0]["filename"], "a.iso")

        st = api.handle(self.ctx, "history-stats", {})
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["count"], 1)
        self.assertEqual(st["bytes"], 2048)
        self.assertEqual(st["hosts"][0]["host"], "ornek.example")

        cleared = api.handle(self.ctx, "history-clear", {"older_than": 3650})
        self.assertTrue(cleared["ok"], cleared)
        self.assertEqual(cleared["removed"], 0, "yeni kayit silinmemeli")
        cleared = api.handle(self.ctx, "history-clear", {})
        self.assertEqual(cleared["removed"], 1)

    def test_17_redownload_via_api(self):
        job = make_job(
            self.env, self.cfg, "rd1", url="https://ornek.example/paket.deb",
            filename="paket.deb", size=4096, links=(2, 5),
            headers={"X-Test": "1"}, cookies="a=b", user="kullanici",
            password="gizli", proxy="", insecure=False,
            referer="https://ornek.example/", user_agent="FerDimTest/1",
            mirrors=["https://ayna.example/paket.deb"],
            sha256="a" * 64, limit=4096, segments=8)
        self.qm.history.record(job)
        rec = self.qm.history.get("rd1")
        self.assertEqual(rec["url"], "https://ornek.example/paket.deb")

        r = api.handle(self.ctx, "history-redownload", {"id": "rd1"})
        self.assertTrue(r.get("ok"), r)
        self.assertNotEqual(r["id"], "rd1")
        self.assertEqual(r.get("history"), "rd1")
        new = self.qm.get(r["id"])
        self.assertIsNotNone(new)
        self.assertEqual(new.spec.url, rec["url"])
        self.assertEqual(new.spec.filename, "paket.deb")
        self.assertEqual(new.spec.mirrors, rec["opts"]["mirrors"])
        self.assertEqual(new.spec.headers.get("X-Test"), "1")
        self.assertEqual(new.spec.user, "kullanici")
        self.assertEqual(new.spec.password, "gizli", "yeniden indirme kimligi korur")
        self.assertEqual(new.spec.sha256, "a" * 64)
        self.assertEqual(new.spec.limit, 4096)

        missing = api.handle(self.ctx, "history-redownload", {"id": "yok"})
        self.assertFalse(missing["ok"])

    def test_18_finished_job_history_shows_in_api(self):
        r = api.handle(self.ctx, "add", {"url": self.base + "/tiny.bin"})
        job = self.qm.get(r["id"])
        self.assertTrue(wait_state(job, (JobState.DONE,)))
        found = api.handle(self.ctx, "history", {"state": "done"})
        self.assertTrue(found["ok"])
        self.assertEqual([i["filename"] for i in found["items"]], ["tiny.bin"])

    def test_18b_active_time_survives_queue_restart(self):
        """Aktif sure queue.json'a yazilir: daemon yeniden baslayinca onceki
        turlarin connecting/downloading saniyeleri kaybolmaz."""
        self.qm.shutdown()                  # bu test kendi kuyrugunu kurar
        qm = QueueManager(self.env, self.cfg)
        try:
            job = qm.add(JobSpec(url="https://ornek.example/x.iso"))
            job.state = JobState.DOWNLOADING
            job._active_mark -= 7.0
            job.state = JobState.PAUSED
            qm._save()
        finally:
            qm.shutdown()
        qm2 = QueueManager(self.env, self.cfg)
        try:
            got = [j for j in qm2.jobs if j.id == job.id][0]
            self.assertEqual(got.state, JobState.PAUSED)
            self.assertAlmostEqual(got.active_seconds(), 7.0, delta=0.2)
        finally:
            qm2.shutdown()


class CliTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, answers):
            self.answers = answers
            self.requests = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, cmd, **params):
            self.requests.append((cmd, params))
            return self.answers.get(cmd, {"ok": True})

    def run_cli(self, argv, answers):
        client = self.FakeClient(answers)
        with mock.patch("ferdim.cli._client", return_value=client):
            args = cli.build_parser().parse_args(["history", *argv])
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.cmd_history(args, None)
        return rc, out.getvalue(), client.requests

    def test_19_cli_search_listing(self):
        items = [{"id": "abc123", "state": "done", "bytes": 2048,
                  "finished_at": time.time(), "filename": "a.iso"}]
        rc, out, req = self.run_cli(
            ["iso", "--state", "done", "--limit", "5"],
            {"history": {"ok": True, "items": items, "total": 1}})
        self.assertEqual(rc, 0)
        self.assertEqual(req[0][0], "history")
        self.assertEqual(req[0][1]["q"], "iso")
        self.assertEqual(req[0][1]["state"], "done")
        self.assertEqual(req[0][1]["limit"], 5)
        self.assertIn("a.iso", out)
        self.assertIn("abc123", out)

    def test_20_cli_stats_clear_redownload(self):
        rc, out, req = self.run_cli(
            ["--stats"], {"history-stats": {
                "ok": True, "count": 2, "bytes": 2048, "size": 2048,
                "duration": 4.0,
                "hosts": [{"host": "a.example", "count": 2, "bytes": 2048,
                           "avg_links": 2.5, "peak_links": 4,
                           "avg_speed": 512.0}]}})
        self.assertEqual(rc, 0)
        self.assertEqual(req[0][0], "history-stats")
        self.assertIn("a.example", out)

        rc, out, req = self.run_cli(
            ["--clear", "--older-than", "30"],
            {"history-clear": {"ok": True, "removed": 7}})
        self.assertEqual(rc, 0)
        self.assertEqual(req[0], ("history-clear", {"older_than": 30}))
        self.assertIn("7", out)

        rc, out, req = self.run_cli(
            ["--redownload", "abc123"],
            {"history-redownload": {"ok": True, "id": "def456",
                                    "filename": "a.iso"}})
        self.assertEqual(rc, 0)
        self.assertEqual(req[0], ("history-redownload", {"id": "abc123"}))
        self.assertIn("def456", out)

    def test_21_cli_failure_exit_code(self):
        rc, out, _ = self.run_cli([], {"history": {"ok": False,
                                                   "error": "gecmis okunamadi"}})
        self.assertEqual(rc, 1)


class ConfigTests(unittest.TestCase):
    def test_22_history_days_sanitised(self):
        cfg = {"history_days": 99999}
        api._sanitize_config(cfg)
        self.assertEqual(cfg["history_days"], 3650)
        cfg = {"history_days": -3}
        api._sanitize_config(cfg)
        self.assertEqual(cfg["history_days"], 0)
        cfg = {"history_days": "bozuk"}
        api._sanitize_config(cfg)
        self.assertEqual(cfg["history_days"], 365)
        self.assertEqual(DEFAULTS["history_days"], 365)


class UiStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent.parent
        cls.js = (cls.root / "web" / "app.js").read_text("utf-8")
        cls.html = (cls.root / "web" / "index.html").read_text("utf-8")

    def test_23_history_panel_textcontent_only(self):
        self.assertNotIn("innerHTML", self.js)
        for needle in ('id="histSearch"', 'id="histList"', 'id="histState"',
                       'id="histStats"', 'id="histClear"',
                       'aria-labelledby="histH"'):
            self.assertIn(needle, self.html)
        for fn in ("loadHistory", "loadHistStats", "redownload"):
            self.assertIn(fn, self.js)
        self.assertIn("historyRow", self.js)
        # dugme etiketi: simge + erisilebilir ad
        self.assertIn('"aria-label": tr("hist_redownload")', self.js)

    def test_24_history_i18n_keys_complete(self):
        keys = [k for k in S if k.startswith("hist_")]
        self.assertGreaterEqual(len(keys), 15)
        for key in keys:
            self.assertEqual(set(S[key]), set(LANGS), key)
        formats = {
            "hist_stats": {"n": 1, "bytes": "1 B"},
            "hist_host_line": {"host": "h", "n": 1, "links": "2.0",
                               "speed": "1 B/sn"},
            "hist_meta": {"state": "x", "size": "1 B", "date": "d", "host": "h"},
            "hist_links": {"avg": "1.0", "peak": 2},
            "hist_redownloaded": {"name": "a.iso"},
            "hist_cleared": {"n": 3},
        }
        for key, kw in formats.items():
            for lang in LANGS:
                text = t(key, lang, **kw)
                self.assertNotIn("{", text, f"{key}/{lang}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
