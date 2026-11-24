"""1.5 testleri: API hata kodlari (cevrilebilir motor mesajlari) ve arka
plan ayna yoklamasi."""
from __future__ import annotations

import errno
import hashlib
import os
import sys
import threading
import time
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim.engine import job as job_mod
from ferdim.engine.job import (DownloadJob, JobSpec, JobState, SourceInvalid,
                               _code_for)
from ferdim.i18n import LANGS, S, error_text
from ferdim.paths import AppEnv
from tests import helpers
from tests.helpers import DATA, DATA_SHA, REQUEST_COUNTS, start_server, tmp_root

CODES = ("not_found", "forbidden", "auth", "source_changed", "no_sources",
         "checksum_mismatch", "disk", "network", "stall", "size_mismatch",
         "incompatible", "rate_limited", "mirror_dead", "unknown",
         "merkle_mismatch", "merkle_manifest")


def wait_for(pred, timeout=30.0, step=0.05):
    """Zaman asimli yoklama: testlerde sabit uyku yok."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(step)
    return False


def wait_state(job, states, timeout=60):
    return wait_for(lambda: job.state in states, timeout, 0.1)


def closed_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def start_flaky_server(code=502):
    """Yoklama istegine (bytes=0-0) gecici 5xx donen, gercek araliklari
    dogru servis eden ayna sunucusu."""
    class Handler(helpers.Handler):
        def do_GET(self, head=False):
            if (self.headers.get("Range") or "") == "bytes=0-0":
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return super().do_GET(head)

    ThreadingHTTPServer.request_queue_size = 128
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class ErrorCodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()
        cls.env = AppEnv(root=tmp_root(cls, "ferdim-err-"))
        cls.cfg = cls.env.load_config()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _job(self, url, **kw):
        return DownloadJob(self.env, self.cfg, JobSpec(url=url, **kw))

    def test_01_404_primary(self):
        """404: kod not_found; mesaj alani degismeden kalir (uyumluluk)."""
        job = self._job(f"{self.base}/yok-bir-dosya.bin")
        job.start()
        self.assertTrue(wait_state(job, (JobState.ERROR,)), job.state)
        self.assertEqual(job.error_code, "not_found")
        self.assertEqual(job.error, "kaynak bulunamadi (HTTP 404)")
        snap = job.snapshot()
        self.assertEqual(snap["error_code"], "not_found")
        self.assertEqual(snap["error"], job.error)
        job.cancel(delete_files=True)

    def test_02_checksum_mismatch(self):
        job = self._job(f"{self.base}/file.bin", segments=2,
                        sha256="0" * 64)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                        job.state)
        self.assertEqual(job.state, JobState.ERROR)
        self.assertEqual(job.error_code, "checksum_mismatch")
        snap = job.snapshot()
        self.assertEqual(snap["error_code"], "checksum_mismatch")
        self.assertEqual(snap["checksum"], "mismatch")
        job.cancel(delete_files=True)

    def test_03_all_sources_dead(self):
        """Calisirken tum kaynaklar olurse kod no_sources olur."""
        job = self._job(f"{self.base}/file.bin")
        job.state = JobState.DOWNLOADING
        job.total = 1000
        self.env.download_dir.mkdir(parents=True, exist_ok=True)
        job._fd = os.open(self.env.download_dir / "dead.part",
                          os.O_RDWR | os.O_CREAT, 0o644)
        job._srcs[0].dead = True
        cooldown, exit_worker = job._process_range(
            (0, 999), job.spec.options(self.cfg))
        self.assertTrue(exit_worker)
        self.assertEqual(job.state, JobState.ERROR)
        self.assertEqual(job.error_code, "no_sources")
        self.assertIn("tum kaynaklar", job.error)
        job.cancel(delete_files=True)

    def test_04_code_mapping(self):
        def http(code):
            return urllib.error.HTTPError("u", code, "x", {}, None)

        self.assertEqual(_code_for(http(404)), "not_found")
        self.assertEqual(_code_for(http(410)), "not_found")
        self.assertEqual(_code_for(http(401)), "auth")
        self.assertEqual(_code_for(http(403)), "forbidden")
        self.assertEqual(_code_for(http(500)), "unknown")
        self.assertEqual(_code_for(TimeoutError("t")), "network")
        self.assertEqual(_code_for(urllib.error.URLError(
            ConnectionRefusedError(111, "yok"))), "network")
        self.assertEqual(_code_for(OSError(errno.ENOSPC, "dolu")), "disk")
        self.assertEqual(_code_for(SourceInvalid("Range yok", "incompatible")),
                         "incompatible")
        self.assertEqual(_code_for(RuntimeError("ozel")), "unknown")

    def test_05_translation_fallback(self):
        self.assertIn("bulunamadı", error_text("not_found", "ham", "tr"))
        self.assertEqual(error_text("not_found", "ham", "en"),
                         S["err_not_found"]["en"])
        self.assertEqual(error_text("", "ham metin", "en"), "ham metin")
        self.assertEqual(error_text("bilinmeyen_kod", "ham metin", "en"),
                         "ham metin")

    def test_06_ui_uses_error_codes(self):
        root = Path(__file__).resolve().parent.parent
        js = (root / "web" / "app.js").read_text("utf-8")
        self.assertIn("error_code", js)
        self.assertIn('"err_" + code', js)
        self.assertNotIn("innerHTML", js)
        cli = (root / "ferdim" / "cli.py").read_text("utf-8")
        self.assertIn("error_text(", cli)


class MirrorProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()
        cls.env = AppEnv(root=tmp_root(cls, "ferdim-probe-"))
        cls.cfg = cls.env.load_config()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _job(self, url, **kw):
        return DownloadJob(self.env, self.cfg, JobSpec(url=url, **kw))

    def test_01_probe_kills_404_mirror_before_any_worker(self):
        """Yoklama isci baslamadan 404 aynayi olur; isci ona hic gitmez."""
        srv_b, base_b = start_server()
        try:
            job = self._job(f"{self.base}/file.bin",
                            mirrors=[f"{base_b}/yok-ayna.bin"])
            job.total = len(DATA)
            port = srv_b.server_address[1]
            before = REQUEST_COUNTS.get(port, 0)
            job._probe_mirrors()          # senkron: isci yok
            mirror = job._srcs[1]
            self.assertTrue(mirror.dead)
            self.assertEqual(mirror.code, "not_found")
            self.assertEqual(REQUEST_COUNTS.get(port, 0) - before, 1)
            health = job.snapshot()["mirror_health"]
            self.assertEqual(health, [{"url": f"{base_b}/yok-ayna.bin",
                                       "state": "dead", "code": "not_found"}])
            self.assertIn("mirror_dead", job.snapshot()["warning_codes"])
            self.assertIs(job._pick_locked(time.monotonic())[0],
                          job._srcs[0], "olu ayna secilmemeli")
        finally:
            srv_b.shutdown()

    def test_02_probe_rejects_other_size(self):
        job = self._job(f"{self.base}/file.bin",
                        mirrors=[f"{self.base}/tiny.bin"])
        job.total = len(DATA)
        job._probe_mirrors()
        self.assertTrue(job._srcs[1].dead)
        self.assertEqual(job._srcs[1].code, "size_mismatch")

    def test_03_probe_rejects_rangeless_mirror(self):
        job = self._job(f"{self.base}/file.bin",
                        mirrors=[f"{self.base}/plain.bin"])
        job.total = len(DATA)
        job._probe_mirrors()
        self.assertTrue(job._srcs[1].dead)
        self.assertEqual(job._srcs[1].code, "incompatible")

    def test_04_background_probe_runs_on_start(self):
        """Is baslarken yoklama arka planda calisir; kotu ayna bildirilir."""
        srv_b, base_b = start_server()
        try:
            job = self._job(f"{self.base}/file.bin", segments=4,
                            mirrors=[f"{base_b}/yok-ayna.bin"])
            job.start()
            ok = wait_for(
                lambda: (job.snapshot()["mirror_health"] or [{}])[0]
                .get("state") == "dead", 30)
            self.assertTrue(ok, "ayna yoklamasi olu isaretlemedi")
            health = job.snapshot()["mirror_health"][0]
            self.assertEqual(health["code"], "not_found")
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)))
            self.assertEqual(job.state, JobState.DONE, job.error)
            self.assertEqual(sha(job.final_path), DATA_SHA)
            self.assertEqual(job.snapshot()["sources_dead"], 1)
        finally:
            srv_b.shutdown()

    def test_05_healthy_mirror_survives_probe(self):
        srv_b, base_b = start_server()
        try:
            job = self._job(f"{self.base}/file.bin", segments=4,
                            mirrors=[f"{base_b}/file.bin"])
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)))
            self.assertEqual(job.state, JobState.DONE, job.error)
            self.assertEqual(job.snapshot()["mirror_health"],
                             [{"url": f"{base_b}/file.bin", "state": "ok",
                               "code": ""}])
        finally:
            srv_b.shutdown()

    def test_06_probe_never_kills_the_serving_source(self):
        """Birincil kapaliyken aynadan indirilirken yoklama onu oldurmez."""
        job = self._job(f"http://127.0.0.1:{closed_port()}/kapali.bin",
                        segments=4, mirrors=[f"{self.base}/file.bin"])
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)))
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path), DATA_SHA)

    def test_07_transient_5xx_probe_does_not_kill_mirror(self):
        """Gecici 5xx yalnizca basarisizlik serisine girer (cooldown);
        ayna olu sayilmaz ve is no_sources ile bitmez."""
        for code in (500, 502):
            with self.subTest(code=code):
                srv_b, base_b = start_flaky_server(code)
                try:
                    job = self._job(f"{self.base}/file.bin", segments=4,
                                    mirrors=[f"{base_b}/file.bin"])
                    job.start()
                    self.assertTrue(wait_for(lambda: job._srcs[1].fails >= 1,
                                             30), "yoklama 5xx kaydetmedi")
                    t = job._probe_t
                    if t is not None:
                        t.join(timeout=10)
                    mirror = job._srcs[1]
                    self.assertFalse(mirror.dead,
                                     "gecici 5xx aynayi oldurmemeli")
                    self.assertGreaterEqual(mirror.fails, 1)
                    self.assertNotIn("mirror_dead",
                                     job.snapshot()["warning_codes"])
                    self.assertTrue(wait_state(job, (JobState.DONE,
                                                     JobState.ERROR)))
                    self.assertEqual(job.state, JobState.DONE, job.error)
                    self.assertNotEqual(job.error_code, "no_sources")
                    self.assertEqual(sha(job.final_path), DATA_SHA)
                    self.assertEqual(job.snapshot()["sources_dead"], 0)
                finally:
                    srv_b.shutdown()

    def test_08_probe_stops_when_job_paused(self):
        """Duraklatma (_stop) sonrasi bekleyen yoklama kaynak kaydini
        degistirmemeli."""
        job = self._job(f"{self.base}/file.bin",
                        mirrors=[f"{self.base}/yok-ayna.bin"])
        job.total = len(DATA)
        job.state = JobState.DOWNLOADING
        entered, release = threading.Event(), threading.Event()

        def blocked(url, opt):
            entered.set()
            release.wait(10)
            raise urllib.error.HTTPError(url, 404, "x", {}, None)

        with mock.patch.object(job_mod.httpcli, "probe", side_effect=blocked):
            job._start_mirror_probe()
            t = job._probe_t
            self.assertIsNotNone(t)
            self.assertTrue(entered.wait(10), "yoklama baslamadi")
            job.pause()
            release.set()
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "yoklama durmadi")
        mirror = job._srcs[1]
        self.assertFalse(mirror.dead,
                         "duraklatilan ista yoklama kaynak oldurdu")
        self.assertEqual(mirror.fails, 0)
        self.assertEqual(job.snapshot()["sources_dead"], 0)

    def test_09_resume_discards_stale_probe(self):
        """Yeni baslatma kaynak listesini degistirir; eski yoklama ne eski
        ne yeni kayitlara dokunur."""
        job = self._job(f"{self.base}/file.bin",
                        mirrors=[f"{self.base}/yok-ayna.bin"])
        job.total = len(DATA)
        job.state = JobState.DOWNLOADING
        old_mirror = job._srcs[1]
        entered, release = threading.Event(), threading.Event()

        def blocked(url, opt):
            entered.set()
            release.wait(10)
            raise urllib.error.HTTPError(url, 404, "x", {}, None)

        with mock.patch.object(job_mod.httpcli, "probe", side_effect=blocked):
            job._start_mirror_probe()
            t = job._probe_t
            self.assertTrue(entered.wait(10), "yoklama baslamadi")
            job._reset_sources()          # start() gibi yeni kusak
            release.set()
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "yoklama durmadi")
        self.assertFalse(old_mirror.dead, "eski kayit degismemeli")
        self.assertEqual(old_mirror.fails, 0)
        self.assertFalse(job._srcs[1].dead, "yeni kayit degismemeli")
        self.assertEqual(job.snapshot()["sources_dead"], 0)


class ErrCodeI18nTests(unittest.TestCase):
    def test_keys_in_all_four_languages(self):
        for code in CODES:
            key = f"err_{code}"
            self.assertIn(key, S, key)
            self.assertEqual(set(S[key]), set(LANGS), key)
            for lang in LANGS:
                self.assertTrue(S[key][lang].strip(), f"{key}/{lang}")

    def test_extension_locales_untouched_by_engine_keys(self):
        """Motor kodlari web/CLI sozlugunde yasar; eklenti sozlugu ayri."""
        root = Path(__file__).resolve().parent.parent
        for lang in LANGS:
            p = root / "extension" / "_locales" / lang / "messages.json"
            self.assertTrue(p.is_file(), p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
