"""HTTP API yol kisitlamasi ve config.json guvenli yazimi.

HTTP uzerinden gelen 'add' / arsiv cikarma istekleri, izinli koklerin
(indirme klasoru, watch_dirs, config.allowed_dirs) disina yazamaz. Unix
soketi (ayni kullanici, 0600) kisitlanmaz: 'ferdim add --dir /mnt/x'
calismaya devam eder. config.json umask'tan bagimsiz 0600 yazilir.
"""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import api
from ferdim.engine.job import JobState
from ferdim.engine.queue import QueueManager
from ferdim.i18n import LANGS, S
from ferdim.paths import DEFAULTS, AppEnv
from tests.helpers import tmp_root


def zip_bytes(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class ConfinementCase(unittest.TestCase):
    def setUp(self):
        self.env = AppEnv(root=tmp_root(self, "ferdim-conf-"))
        self.cfg = self.env.load_config()
        self.qm = QueueManager(self.env, self.cfg)
        self.addCleanup(self.qm.shutdown)
        self.ctx = {"qm": self.qm, "env": self.env, "cfg": self.cfg,
                    "set_cfg": self.env.save_config}
        # HTTP sunucusunun gecirdigi bayrak; Unix soketi bunu gecmez
        self.restricted = {**self.ctx, "restrict_paths": True}
        self.outside = tmp_root(self, "ferdim-outside-")

    # ------------------------------------------------------------- helpers
    def _add(self, ctx=None, **params):
        return api.handle(ctx or self.restricted, "add", dict(params))

    def _done_job(self, name: str, data: bytes, *, ctx=None,
                  directory=None) -> object:
        params = {"url": "http://127.0.0.1:1/" + name, "filename": name,
                  "paused": True}
        if directory is not None:
            params["dir"] = str(directory)
        r = self._add(ctx or self.ctx, **params)
        self.assertTrue(r.get("ok"), r)
        job = self.qm.get(r["id"])
        path = job.final_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        job.state = JobState.DONE
        return job

    def _wait(self, task: str) -> dict:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            res = api.handle(self.ctx, "archive-status", {"task": task})
            self.assertTrue(res.get("ok"), res)
            if res.get("state") != "running":
                return res
            time.sleep(0.02)
        self.fail("arsiv gorevi bitmedi")

    def assertDenied(self, res, msg=""):
        self.assertFalse(res.get("ok"), (msg, res))
        self.assertEqual(res.get("code"), "dir_not_allowed", (msg, res))
        self.assertIn("dizin", res.get("error", ""), msg)


class AddDirTests(ConfinementCase):
    def test_inside_downloads_is_allowed(self):
        target = self.env.download_dir / "alt"
        r = self._add(url="http://127.0.0.1:1/a.bin", filename="a.bin",
                      dir=str(target), paused=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.qm.get(r["id"]).spec.directory, str(target))

    def test_directory_alias_is_confined_too(self):
        target = self.env.download_dir / "alias"
        r = self._add(url="http://127.0.0.1:1/a.bin", filename="a.bin",
                      directory=str(target), paused=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.qm.get(r["id"]).spec.directory, str(target))

    def test_outside_root_is_rejected(self):
        self.assertDenied(self._add(
            url="http://127.0.0.1:1/a.bin", filename="a.bin",
            dir=str(self.outside), paused=True))
        self.assertEqual([j for j in self.qm.jobs
                          if j.spec.directory == str(self.outside)], [])

    def test_sibling_prefix_is_not_a_root(self):
        """Naif startswith kontrolu '/downloads2' yolunu gecirirdi."""
        sibling = str(self.env.download_dir) + "2"
        self.assertDenied(self._add(
            url="http://127.0.0.1:1/a.bin", filename="a.bin",
            dir=sibling, paused=True))

    def test_symlink_escaping_root_is_rejected(self):
        self.env.download_dir.mkdir(parents=True, exist_ok=True)
        link = self.env.download_dir / "kacis"
        os.symlink(self.outside, link)
        self.assertDenied(self._add(
            url="http://127.0.0.1:1/a.bin", filename="a.bin",
            dir=str(link / "alt"), paused=True))

    def test_relative_path_is_rejected(self):
        self.assertDenied(self._add(
            url="http://127.0.0.1:1/a.bin", filename="a.bin",
            dir="goreli/dizin", paused=True))

    def test_whitespace_prefixed_absolute_path_is_relative(self):
        # basa bosluk koymak yolu goreli yapar; dogrulama yine reddetmeli
        self.assertDenied(self._add(
            url="http://127.0.0.1:1/a.bin", filename="a.bin",
            dir=" " + str(self.env.download_dir / "a"), paused=True))

    def test_nul_byte_is_rejected(self):
        self.assertDenied(self._add(
            url="http://127.0.0.1:1/a.bin", filename="a.bin",
            dir=str(self.env.download_dir / "a\x00b"), paused=True))

    def test_no_dir_uses_default_root(self):
        r = self._add(url="http://127.0.0.1:1/a.bin", filename="a.bin",
                      paused=True)
        self.assertTrue(r["ok"], r)

    def test_watch_dir_is_an_allowed_root(self):
        watch = self.outside / "watch"
        watch.mkdir(parents=True)
        self.cfg["watch_dirs"] = [str(watch)]
        r = self._add(url="http://127.0.0.1:1/a.bin", filename="a.bin",
                      dir=str(watch / "alt"), paused=True)
        self.assertTrue(r["ok"], r)

    def test_allowed_dirs_root(self):
        extra = self.outside / "serbest"
        extra.mkdir(parents=True)
        self.cfg["allowed_dirs"] = [str(extra)]
        r = self._add(url="http://127.0.0.1:1/a.bin", filename="a.bin",
                      dir=str(extra / "alt"), paused=True)
        self.assertTrue(r["ok"], r)

    def test_unrestricted_socket_keeps_working(self):
        r = self._add(self.ctx, url="http://127.0.0.1:1/a.bin",
                      filename="a.bin", dir=str(self.outside), paused=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.qm.get(r["id"]).spec.directory,
                         str(self.outside))

    def test_add_batch_rejects_only_the_bad_item(self):
        good = self.env.download_dir / "iyi"
        r = api.handle(self.restricted, "add-batch", {"items": [
            {"url": "http://127.0.0.1:1/iyi.bin", "filename": "iyi.bin",
             "dir": str(good)},
            {"url": "http://127.0.0.1:1/kotu.bin", "filename": "kotu.bin",
             "dir": str(self.outside)},
        ]})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(len(r["ids"]), 1, r)
        dirs = [j.spec.directory for j in self.qm.jobs]
        self.assertIn(str(good), dirs)
        self.assertNotIn(str(self.outside), dirs)

    def test_history_redownload_confines_stored_dir(self):
        rec = {"id": "h1", "url": "http://127.0.0.1:1/eski.bin",
               "opts": {"dir": str(self.outside), "filename": "eski.bin"}}
        with mock.patch.object(self.qm.history, "get", return_value=rec):
            r = api.handle(self.restricted, "history-redownload", {"id": "h1"})
        self.assertDenied(r, "gecmisten gelen dizin")


class ArchiveConfinementTests(ConfinementCase):
    def _zip_job(self):
        return self._done_job("paket.zip", zip_bytes(
            {"a.txt": b"a" * 10}))

    def test_extract_dest_outside_is_rejected(self):
        job = self._zip_job()
        for cmd in ("archive-extract", "archive-repair"):
            self.assertDenied(api.handle(self.restricted, cmd, {
                "id": job.id, "members": [], "dest": str(self.outside)}), cmd)

    def test_extract_dest_inside_is_allowed(self):
        job = self._zip_job()
        dest = self.env.download_dir / "cikti"
        started = api.handle(self.restricted, "archive-extract", {
            "id": job.id, "members": [], "dest": str(dest)})
        self.assertTrue(started["ok"], started)
        done = self._wait(started["task"])
        self.assertEqual(done["state"], "done", done)
        self.assertEqual((dest / "a.txt").read_bytes(), b"a" * 10)

    def test_default_dest_beside_archive_is_allowed(self):
        job = self._zip_job()
        started = api.handle(self.restricted, "archive-extract",
                             {"id": job.id, "members": []})
        self.assertTrue(started["ok"], started)
        self.assertEqual(Path(started["dest"]).parent,
                         self.env.download_dir)
        done = self._wait(started["task"])
        self.assertEqual(done["state"], "done", done)

    def test_archive_itself_outside_root_is_rejected(self):
        # soket tarafi diledigi dizine is birakabilir; HTTP tarafi o isi
        # cikarmak icin kullanirsa arsivin kendisi de kokte olmali
        job = self._zip_job()
        outside_job = self._done_job(
            "disari.zip", zip_bytes({"b.txt": b"b"}), directory=self.outside)
        self.assertEqual(outside_job.final_path.parent, self.outside)
        self.assertDenied(api.handle(self.restricted, "archive-extract", {
            "id": outside_job.id, "members": [],
            "dest": str(self.env.download_dir / "ok")}), "kaynak disarida")
        # kokteki arsiv ayni istekle gecer
        ok = api.handle(self.restricted, "archive-extract", {
            "id": job.id, "members": [],
            "dest": str(self.env.download_dir / "ok")})
        self.assertTrue(ok["ok"], ok)
        self._wait(ok["task"])

    def test_unrestricted_archive_dest_keeps_working(self):
        job = self._zip_job()
        dest = self.outside / "soket-cikti"
        started = api.handle(self.ctx, "archive-extract", {
            "id": job.id, "members": [], "dest": str(dest)})
        self.assertTrue(started["ok"], started)
        done = self._wait(started["task"])
        self.assertEqual(done["state"], "done", done)
        self.assertTrue((dest / "a.txt").is_file())


class ConfigWriteTests(unittest.TestCase):
    def setUp(self):
        self.env = AppEnv(root=tmp_root(self, "ferdim-cfg-"))
        self.cfg = self.env.load_config()

    def test_save_config_is_0600_regardless_of_umask(self):
        old = os.umask(0o022)
        try:
            self.env.save_config(self.cfg)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(self.env.config_path.stat().st_mode),
                         0o600)
        self.assertEqual(stat.S_IMODE(self.env.config_dir.stat().st_mode),
                         0o700)
        text = self.env.config_path.read_text("utf-8")
        self.assertIn("\n  ", text, "config okunabilir (indent=2) kalmali")
        self.assertEqual(json.loads(text), self.cfg)

    def test_save_config_replaces_a_stale_tmp_file(self):
        tmp = self.env.config_path.with_suffix(".json.tmp")
        tmp.write_text("yarim", "utf-8")
        self.env.save_config(self.cfg)
        self.assertFalse(tmp.exists(), "eski gecici dosya temizlenmeli")
        self.assertEqual(json.loads(self.env.config_path.read_text("utf-8")),
                         self.cfg)


class AllowedDirsSanitizeTests(unittest.TestCase):
    def test_default_is_empty(self):
        self.assertEqual(DEFAULTS["allowed_dirs"], [])

    def test_relative_and_non_string_entries_are_dropped(self):
        root = tmp_root(self, "ferdim-adirs-")
        cfg = dict(DEFAULTS)
        cfg["allowed_dirs"] = [str(root / "ok"), "goreli", 5, "",
                               str(root / "nul\x00x"), "/abs/yol"]
        api._sanitize_config(cfg)
        self.assertEqual(cfg["allowed_dirs"], [str(root / "ok"), "/abs/yol"])

    def test_at_most_32_entries(self):
        root = tmp_root(self, "ferdim-adirs-")
        cfg = dict(DEFAULTS)
        cfg["allowed_dirs"] = [str(root / f"d{i:02d}") for i in range(40)]
        api._sanitize_config(cfg)
        self.assertEqual(len(cfg["allowed_dirs"]), api.MAX_ALLOWED_DIRS)
        self.assertTrue(all(os.path.isabs(d) for d in cfg["allowed_dirs"]))

    def test_non_list_becomes_empty(self):
        cfg = dict(DEFAULTS)
        cfg["allowed_dirs"] = "/tek/yol"
        api._sanitize_config(cfg)
        self.assertEqual(cfg["allowed_dirs"], [])

    def test_error_key_in_all_four_languages(self):
        entry = S["err_dir_not_allowed"]
        self.assertEqual(set(entry), set(LANGS))
        for lang in LANGS:
            self.assertTrue(entry[lang].strip(), lang)


if __name__ == "__main__":
    unittest.main(verbosity=2)
