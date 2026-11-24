"""ROADMAP 1.6: arsiv tarama (ic ice ZIP), secmeli cikarma ve onarim.

Guvenlik testleri kacis denemelerinin (.., mutlak yol, baglanti) hedef
dizin disina hicbir sey yazmadigini dogrular; boyut/oran sinirlari
kucultulmus sabitlerle (mock.patch) sinanir."""
from __future__ import annotations

import argparse
import collections
import contextlib
import io
import os
import sys
import tarfile
import time
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import api, archive, cli, util
from ferdim.engine.job import JobSpec, JobState
from ferdim.engine.queue import QueueManager
from ferdim.paths import AppEnv
from tests.helpers import tmp_root


def zip_bytes(members: dict, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def make_zip(path, members: dict, compression=zipfile.ZIP_DEFLATED):
    Path(path).write_bytes(zip_bytes(members, compression))
    return Path(path)


def make_tar(path, members: dict, mode: str = "w:gz"):
    with tarfile.open(path, mode) as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            tf.addfile(info, io.BytesIO(data))
    return Path(path)


def flag_encrypted(data: bytes, member: str) -> bytes:
    """ZIP baytlarinda `member` icin sifreleme bitini (bit 0) acar: hem
    yerel baslikta (offset 6) hem merkezi dizinde (offset 8)."""
    buf = bytearray(data)
    for sig, at in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        off = 0
        while True:
            off = buf.find(sig, off)
            if off < 0:
                break
            nlen_at = off + (26 if at == 6 else 28)
            nlen = int.from_bytes(buf[nlen_at:nlen_at + 2], "little")
            name_at = off + (30 if at == 6 else 46)
            if bytes(buf[name_at:name_at + nlen]) == member.encode():
                buf[off + at] |= 0x01
            off += 4
    return bytes(buf)


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        self.root = tmp_root(self, "ferdim-arc-")

    def tearDown(self):
        with archive._TLOCK:
            archive._TASKS.clear()

    def paths(self, info: dict) -> dict:
        return {e["path"]: e for e in info["entries"]}

    def wait_task(self, task_id: str, timeout: float = 20) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = archive.task_status(task_id)
            if status.get("ok") and status.get("state") != "running":
                return status
            time.sleep(0.02)
        self.fail("arsiv gorevi bitmedi")


class ListingTests(ArchiveCase):
    def test_01_nested_zip_listing(self):
        inner = zip_bytes({"docs/c.txt": b"c" * 20, "d.bin": b"d" * 40})
        outer = make_zip(self.root / "outer.zip",
                         {"a.txt": b"a" * 10, "sub/b.bin": b"b" * 20,
                          "inner.zip": inner})
        got = self.paths(archive.describe(outer))
        self.assertEqual(set(got), {
            "outer.zip!/a.txt", "outer.zip!/sub/b.bin",
            "outer.zip!/inner.zip", "outer.zip!/inner.zip!/docs/c.txt",
            "outer.zip!/inner.zip!/d.bin"})
        self.assertEqual(got["outer.zip!/a.txt"]["depth"], 1)
        self.assertEqual(got["outer.zip!/inner.zip!/d.bin"]["depth"], 2)
        self.assertEqual(got["outer.zip!/inner.zip!/d.bin"]["size"], 40)
        self.assertEqual(got["outer.zip!/a.txt"]["kind"], "file")

    def test_02_nested_listing_can_be_disabled(self):
        outer = make_zip(self.root / "outer.zip", {
            "a.txt": b"a", "inner.zip": zip_bytes({"in.txt": b"i"})})
        flat = archive.describe(outer, nested=False)
        self.assertTrue(all("inner.zip!" not in e["path"]
                            for e in flat["entries"]))
        self.assertEqual(flat["format"], "zip")

    def test_03_depth_limit(self):
        level4 = zip_bytes({"deep.txt": b"x"})
        level3 = zip_bytes({"l4.zip": level4})
        level2 = zip_bytes({"l3.zip": level3})
        outer = make_zip(self.root / "outer.zip", {"l2.zip": level2})
        info = archive.describe(outer)
        paths = self.paths(info)
        self.assertEqual({e["depth"] for e in info["entries"]}, {1, 2, 3})
        self.assertIn("outer.zip!/l2.zip!/l3.zip!/l4.zip", paths)
        # l4 derinlik sinirinda acilmaz
        self.assertFalse(any("l4.zip!" in p for p in paths))
        self.assertNotIn("outer.zip!/l2.zip!/l3.zip!/l4.zip!/deep.txt", paths)

    def test_04_entry_limit(self):
        members = {f"f{i:02d}.bin": b"x" * 8 for i in range(10)}
        outer = make_zip(self.root / "many.zip", members)
        with mock.patch.object(archive, "MAX_ENTRIES", 5):
            info = archive.describe(outer)
        self.assertEqual(len(info["entries"]), 5)
        self.assertEqual(info["note"], "+5…")

    def test_05_inner_archive_size_cap(self):
        inner = zip_bytes({"in.txt": b"i" * 400})
        outer = make_zip(self.root / "outer.zip", {"inner.zip": inner,
                                                   "a.txt": b"a"})
        with mock.patch.object(archive, "MAX_INNER", 64):
            info = archive.describe(outer)
        paths = self.paths(info)
        self.assertEqual(set(paths), {"outer.zip!/inner.zip",
                                      "outer.zip!/a.txt"})
        self.assertTrue(paths["outer.zip!/inner.zip"].get("note"))

    def test_06_compression_ratio_guard(self):
        bomb = zip_bytes({"in.txt": b"i" * 5})
        outer = make_zip(self.root / "outer.zip", {
            "zeros.bin": b"\0" * 200_000, "bomb.zip": bomb})
        with mock.patch.object(archive, "RATIO_MAX", 1.0), \
                mock.patch.object(archive, "RATIO_MIN_SIZE", 100):
            info = archive.describe(outer)
        paths = self.paths(info)
        self.assertTrue(paths["outer.zip!/zeros.bin"]["bomb"])
        # ic arsiv de oran sinirinda acilmaz
        self.assertEqual(paths["outer.zip!/bomb.zip"]["kind"], "file")
        self.assertNotIn("outer.zip!/bomb.zip!/in.txt", paths)
        self.assertTrue(paths["outer.zip!/bomb.zip"].get("note"))

    def test_07_tar_variants_listing(self):
        members = {"a.txt": b"a" * 12, "sub/b.txt": b"b" * 24,
                   "inner.zip": zip_bytes({"in.txt": b"i" * 5})}
        for mode, suffix in (("w:gz", ".tar.gz"), ("w:bz2", ".tar.bz2"),
                             ("w:xz", ".tar.xz"), ("w", ".tar")):
            with self.subTest(mode=mode):
                path = make_tar(self.root / ("x" + suffix), members, mode)
                info = archive.describe(path)
                self.assertEqual(info["format"], "tar")
                paths = self.paths(info)
                self.assertIn(f"x{suffix}!/a.txt", paths)
                self.assertIn(f"x{suffix}!/inner.zip!/in.txt", paths)

    def test_08_unsupported_format(self):
        bad = self.root / "arsiv.7z"
        bad.write_bytes(b"7z\xbc\xaf\x27\x1c bozuk")
        with self.assertRaises(archive.ArchiveError):
            archive.describe(bad)
        entries, note = util.zip_preview(bad)
        self.assertIsNone(entries)
        self.assertTrue(note)
        entries, note = util.zip_preview(self.root / "yok.zip")
        self.assertIsNone(entries)


class ExtractSafetyTests(ArchiveCase):
    def test_10_selective_extract(self):
        arc = make_zip(self.root / "outer.zip", {
            "a.txt": b"a" * 10, "b.txt": b"b" * 20,
            "inner.zip": zip_bytes({"in.txt": b"i" * 5})})
        dest = self.root / "out"
        res = archive.extract(arc, members=["outer.zip!/b.txt"], dest=dest)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["extracted"], 1)
        self.assertEqual((dest / "b.txt").read_bytes(), b"b" * 20)
        self.assertFalse((dest / "a.txt").exists())
        self.assertFalse((dest / "inner").exists())
        self.assertEqual(list(dest.rglob("*.part")), [])
        manifest = archive.load_manifest(dest)
        self.assertEqual(set(manifest["members"]), {"b.txt"})
        self.assertEqual(manifest["members"]["b.txt"]["crc"],
                         zlib.crc32(b"b" * 20))

    def test_11_default_dest_next_to_archive(self):
        arc = make_zip(self.root / "paket.tar.zip", {"a.txt": b"a"})
        res = archive.extract(arc)
        self.assertTrue(res["ok"], res)
        self.assertEqual(Path(res["dest"]), self.root / "paket.tar")
        self.assertEqual((self.root / "paket.tar" / "a.txt").read_bytes(), b"a")

    def test_12_nested_member_extract(self):
        arc = make_zip(self.root / "outer.zip", {
            "inner.zip": zip_bytes({"docs/c.txt": b"c" * 7})})
        dest = self.root / "out"
        res = archive.extract(arc, members=["outer.zip!/inner.zip!/docs/c.txt"],
                              dest=dest)
        self.assertTrue(res["ok"], res)
        self.assertEqual((dest / "inner" / "docs" / "c.txt").read_bytes(),
                         b"c" * 7)

    def test_13_zip_traversal_rejected(self):
        arc = make_zip(self.root / "bad.zip", {
            "../evil.txt": b"x",
            str(self.root / "abs.txt"): b"y",
            "a/../../deep.txt": b"w",
            "ok.txt": b"z"})
        dest = self.root / "work" / "out"
        res = archive.extract(arc, dest=dest)
        self.assertFalse(res["ok"])
        self.assertEqual((dest / "ok.txt").read_bytes(), b"z")
        self.assertFalse((self.root / "work" / "evil.txt").exists())
        self.assertFalse((self.root / "abs.txt").exists())
        self.assertFalse((self.root / "deep.txt").exists())
        self.assertGreaterEqual(len(res["errors"]), 3)
        self.assertEqual(list(dest.rglob("*.part")), [])

    def test_14_tar_traversal_and_links_skipped(self):
        path = self.root / "bad.tar"
        with tarfile.open(path, "w") as tf:
            def add(name, data=b""):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = 0
                tf.addfile(info, io.BytesIO(data) if data else None)
            add("ok.txt", b"z")
            add("../evil.txt", b"x")
            sym = tarfile.TarInfo("link")
            sym.type = tarfile.SYMTYPE
            sym.linkname = "/etc/passwd"
            tf.addfile(sym)
            hard = tarfile.TarInfo("hard")
            hard.type = tarfile.LNKTYPE
            hard.linkname = "ok.txt"
            tf.addfile(hard)
        dest = self.root / "work" / "out"
        res = archive.extract(path, dest=dest)
        self.assertFalse(res["ok"])
        self.assertEqual((dest / "ok.txt").read_bytes(), b"z")
        self.assertFalse((self.root / "work" / "evil.txt").exists())
        self.assertFalse((dest / "link").exists())
        self.assertFalse((dest / "link").is_symlink())
        self.assertFalse((dest / "hard").exists())
        self.assertGreaterEqual(res["skipped"], 1)     # baglantilar atlandi

    def test_15_total_size_and_disk_caps(self):
        arc = make_zip(self.root / "outer.zip", {"a.txt": b"a" * 40})
        with mock.patch.object(archive, "MAX_TOTAL", 10):
            res = archive.extract(arc, dest=self.root / "cap")
        self.assertFalse(res["ok"])
        self.assertIn("siniri", res["error"])
        usage = collections.namedtuple("usage", "total used free")
        with mock.patch.object(archive.shutil, "disk_usage",
                               lambda p: usage(1, 1, 0)):
            res = archive.extract(arc, dest=self.root / "disk")
        self.assertFalse(res["ok"])
        self.assertIn("disk", res["error"])
        self.assertFalse((self.root / "disk" / "a.txt").exists())

    def test_16_repair_re_extracts_only_damaged(self):
        arc = make_zip(self.root / "outer.zip", {
            "a.txt": b"a" * 100, "b.txt": b"b" * 200, "c.txt": b"c" * 300})
        dest = self.root / "out"
        self.assertTrue(archive.extract(arc, dest=dest)["ok"])
        keep_inode = (dest / "b.txt").stat().st_ino
        # biri bozuk (ayni boyut, farkli CRC), biri silinmis
        (dest / "a.txt").write_bytes(b"z" * 100)
        (dest / "c.txt").unlink()
        res = archive.extract(arc, dest=dest, repair=True)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["repair"])
        self.assertEqual(res["extracted"], 2)
        self.assertEqual(res["skipped"], 1)
        self.assertEqual((dest / "a.txt").read_bytes(), b"a" * 100)
        self.assertEqual((dest / "c.txt").read_bytes(), b"c" * 300)
        self.assertEqual((dest / "b.txt").stat().st_ino, keep_inode)
        # her sey saglamken onarim hicbir sey yazmaz
        again = archive.extract(arc, dest=dest, repair=True)
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["extracted"], 0)
        self.assertEqual(again["skipped"], 3)

    def test_17_repair_without_manifest(self):
        arc = make_zip(self.root / "outer.zip", {"a.txt": b"a"})
        res = archive.extract(arc, dest=self.root / "yok", repair=True)
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("code"), "no_manifest")

    def test_18_failed_member_keeps_manifest_written(self):
        """Kesilen cikarma: planlanan uyeler kayitta kalir, onarim tamamlar."""
        arc = make_zip(self.root / "outer.zip", {"a.txt": b"a", "b.txt": b"b"})
        dest = self.root / "out"
        real = archive._write_part
        calls = {"n": 0}

        def flaky(stream, part, budget):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("kesildi")
            return real(stream, part, budget)

        with mock.patch.object(archive, "_write_part", flaky):
            res = archive.extract(arc, dest=dest)
        self.assertFalse(res["ok"])
        self.assertEqual(res["extracted"], 1)
        manifest = archive.load_manifest(dest)
        self.assertEqual(set(manifest["members"]), {"a.txt", "b.txt"})
        done = archive.extract(arc, dest=dest, repair=True)
        self.assertTrue(done["ok"], done)
        self.assertEqual(done["extracted"], 1)
        self.assertEqual(done["skipped"], 1)
        self.assertEqual((dest / "a.txt").read_bytes(), b"a")
        self.assertEqual((dest / "b.txt").read_bytes(), b"b")

    def test_19_extract_not_capped_by_listing_limit(self):
        """Liste siniri (MAX_ENTRIES) cikarmayi kismamali."""
        members = {f"f{i:02d}.bin": bytes([i]) * 4 for i in range(6)}
        arc = make_zip(self.root / "many.zip", members)
        dest = self.root / "out"
        with mock.patch.object(archive, "MAX_ENTRIES", 2):
            info = archive.describe(arc)
            self.assertEqual(len(info["entries"]), 2)
            res = archive.extract(arc, dest=dest)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["extracted"], 6)
        self.assertEqual(len(archive.load_manifest(dest)["members"]), 6)
        self.assertEqual(sorted(p.name for p in dest.iterdir()
                                if p.name != archive.MANIFEST),
                         sorted(members))

    def test_20_extract_entry_cap_fails_loudly(self):
        members = {f"f{i}.bin": b"x" for i in range(5)}
        arc = make_zip(self.root / "many.zip", members)
        dest = self.root / "out"
        with mock.patch.object(archive, "MAX_EXTRACT_ENTRIES", 3):
            res = archive.extract(arc, dest=dest)
        self.assertFalse(res["ok"])
        self.assertIn("sinirindan fazla", res["error"])
        self.assertFalse(dest.exists())

    def test_21_tar_extraction_opens_archive_once(self):
        """tar.gz uye basina yeniden acilmaz: acma sayisi sabit."""
        members = {f"f{i}.bin": b"x" * 100 for i in range(8)}
        arc = make_tar(self.root / "cok.tar.gz", members)
        real = tarfile.open
        calls = {"n": 0}

        def counting(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        with mock.patch.object(tarfile, "open", counting):
            res = archive.extract(arc, dest=self.root / "out")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["extracted"], 8)
        self.assertLessEqual(calls["n"], 4, "uye basina yeniden acilmamali")
        # uye sayisi artsa da acma sayisi artmaz
        calls["n"] = 0
        members2 = {f"g{i}.bin": b"y" * 50 for i in range(24)}
        arc2 = make_tar(self.root / "cok2.tar.gz", members2)
        with mock.patch.object(tarfile, "open", counting):
            res = archive.extract(arc2, dest=self.root / "out2")
        self.assertTrue(res["ok"], res)
        self.assertLessEqual(calls["n"], 4)

    def test_22_unowned_existing_files_are_not_overwritten(self):
        arc = make_zip(self.root / "outer.zip", {"a.txt": b"arsiv",
                                                 "b.txt": b"b"})
        dest = self.root / "out"
        dest.mkdir()
        (dest / "a.txt").write_bytes(b"kullanici")
        res = archive.extract(arc, dest=dest)
        self.assertFalse(res["ok"])
        self.assertEqual((dest / "a.txt").read_bytes(), b"kullanici")
        self.assertEqual((dest / "b.txt").read_bytes(), b"b")
        self.assertTrue(any("force" in e for e in res["errors"]), res)
        forced = archive.extract(arc, dest=dest, force=True)
        self.assertTrue(forced["ok"], forced)
        self.assertEqual((dest / "a.txt").read_bytes(), b"arsiv")
        # kendi kaydimizdaki dosyalar force olmadan da guncellenir
        (dest / "b.txt").write_bytes(b"bozuldu")
        again = archive.extract(arc, dest=dest)
        self.assertTrue(again["ok"], again)
        self.assertEqual((dest / "b.txt").read_bytes(), b"b")

    def test_23_literal_bang_in_member_name_is_not_nesting(self):
        arc = make_zip(self.root / "outer.zip", {
            "kutu.zip": zip_bytes({"notlar.txt": b"ic"}),
            "kutu.zip!notlar.txt": b"duz"})
        info = self.paths(archive.describe(arc))
        self.assertIn("outer.zip!/kutu.zip!notlar.txt", info)    # duz uye
        self.assertIn("outer.zip!/kutu.zip!/notlar.txt", info)   # gercek ic
        dest = self.root / "out"
        res = archive.extract(arc, dest=dest)
        self.assertTrue(res["ok"], res)
        self.assertEqual((dest / "kutu.zip!notlar.txt").read_bytes(), b"duz")
        self.assertEqual((dest / "kutu" / "notlar.txt").read_bytes(), b"ic")
        sel = archive.extract(arc, members=["outer.zip!/kutu.zip!notlar.txt"],
                              dest=self.root / "sel")
        self.assertTrue(sel["ok"], sel)
        self.assertEqual(sel["extracted"], 1)
        self.assertEqual((self.root / "sel" / "kutu.zip!notlar.txt")
                         .read_bytes(), b"duz")

    def test_24_repair_uses_archive_crc(self):
        arc = make_zip(self.root / "outer.zip", {"a.txt": b"a" * 50})
        dest = self.root / "out"
        self.assertTrue(archive.extract(arc, dest=dest)["ok"])
        manifest = archive.load_manifest(dest)
        del manifest["members"]["a.txt"]["crc"]      # eski surum kaydi
        archive._save_manifest(dest, manifest)
        (dest / "a.txt").write_bytes(b"z" * 50)      # ayni boyut, bozuk
        res = archive.extract(arc, dest=dest, repair=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["extracted"], 1)
        self.assertEqual((dest / "a.txt").read_bytes(), b"a" * 50)
        self.assertEqual(archive.load_manifest(dest)["members"]["a.txt"]["crc"],
                         zlib.crc32(b"a" * 50))
        # kayittaki CRC bayat ama dosya saglamsa: yazmadan CRC tazelenir
        manifest = archive.load_manifest(dest)
        manifest["members"]["a.txt"]["crc"] = 12345
        archive._save_manifest(dest, manifest)
        again = archive.extract(arc, dest=dest, repair=True)
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["extracted"], 0)
        self.assertEqual(archive.load_manifest(dest)["members"]["a.txt"]["crc"],
                         zlib.crc32(b"a" * 50))

    def test_25_running_task_is_not_pruned(self):
        arc = make_zip(self.root / "a.zip", {"a.txt": b"a"})
        with mock.patch.object(archive, "MAX_TASKS", 1):
            with archive._TLOCK:
                archive._TASKS.clear()
                archive._TASKS["calisan"] = {"id": "calisan",
                                             "state": "running", "started": 0}
            with self.assertRaises(archive.ArchiveError):
                archive.start_extract(arc, [], self.root / "x")
            self.assertIn("calisan", archive._TASKS)
            with archive._TLOCK:
                archive._TASKS["calisan"]["state"] = "done"
                archive._TASKS["calisan"]["started"] = 1
            snap = archive.start_extract(arc, [], self.root / "x")
            self.assertTrue(snap["ok"], snap)
            self.assertNotIn("calisan", archive._TASKS)
            status = self.wait_task(snap["id"])
            self.assertEqual(status["state"], "done")
            self.assertIn(snap["id"], archive._TASKS)


class EncryptedMemberTests(ArchiveCase):
    """Sifreli uye: zipfile RuntimeError atar ("password required"). Bu
    tek uyenin hatasi olmali; cikarmanin tamami dusmemeli."""

    def _zip(self, encrypted: str) -> Path:
        data = zip_bytes({"a.txt": b"a" * 16, "b.txt": b"b" * 16,
                          "inner.zip": zip_bytes({"c.txt": b"c" * 8})})
        path = self.root / "enc.zip"
        path.write_bytes(flag_encrypted(data, encrypted))
        return path

    def test_30_encrypted_member_is_a_per_member_error(self):
        arc = self._zip("b.txt")
        with self.assertRaises(RuntimeError):   # bozulmanin gercekten
            with zipfile.ZipFile(arc) as zf:    # sifreleme oldugunu dogrula
                zf.open("b.txt")
        dest = self.root / "out"
        res = archive.extract(arc, dest=dest)
        self.assertFalse(res["ok"])
        self.assertEqual(res["extracted"], 3)   # digerleri cikarildi
        self.assertTrue(any("b.txt" in err for err in res["errors"]),
                        res["errors"])
        self.assertEqual((dest / "a.txt").read_bytes(), b"a" * 16)
        self.assertFalse((dest / "b.txt").exists())
        self.assertFalse((dest / "b.txt.part").exists())
        # secmeli cikarma da ayni sekilde tek uyeyle sinirli kalir
        sel = archive.extract(arc, members=["b.txt"], dest=self.root / "sel")
        self.assertFalse(sel["ok"])
        self.assertEqual(sel["extracted"], 0)
        self.assertTrue(any("b.txt" in err for err in sel["errors"]),
                        sel["errors"])

    def test_31_encrypted_inner_archive_is_a_note(self):
        """Sifreli ic arsiv listelemede not olur, describe() dusmez."""
        arc = self._zip("inner.zip")
        info = archive.describe(arc)
        inner = {e["path"]: e for e in info["entries"]}[
            "enc.zip!/inner.zip"]
        self.assertIn("okunamaz uye", inner.get("note", ""))
        res = archive.extract(arc, dest=self.root / "out2")
        self.assertFalse(res["ok"])
        self.assertEqual((self.root / "out2" / "a.txt").read_bytes(),
                         b"a" * 16)


class ApiArchiveTests(ArchiveCase):
    def setUp(self):
        super().setUp()
        self.env = AppEnv(root=tmp_root(self, "ferdim-arq-"))
        self.cfg = self.env.load_config()
        self.qm = QueueManager(self.env, self.cfg)
        self.ctx = {"qm": self.qm, "env": self.env, "cfg": self.cfg,
                    "set_cfg": self.env.save_config}

    def tearDown(self):
        self.qm.shutdown()
        super().tearDown()

    def _job(self, name: str, data: bytes):
        job = self.qm.add(JobSpec(url="http://127.0.0.1:1/" + name,
                                  filename=name), paused=True)
        path = job.final_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        job.state = JobState.DONE
        return job, path

    def _wait(self, task: str) -> dict:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            res = api.handle(self.ctx, "archive-status", {"task": task})
            self.assertTrue(res.get("ok"), res)
            if res.get("state") != "running":
                return res
            time.sleep(0.02)
        self.fail("arsiv gorevi bitmedi")

    def test_20_api_list_nested(self):
        job, _ = self._job("outer.zip", zip_bytes({
            "a.txt": b"a", "inner.zip": zip_bytes({"in.txt": b"i"})}))
        res = api.handle(self.ctx, "archive-list",
                         {"id": job.id, "nested": True})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["format"], "zip")
        self.assertEqual({e["path"] for e in res["entries"]}, {
            "outer.zip!/a.txt", "outer.zip!/inner.zip",
            "outer.zip!/inner.zip!/in.txt"})
        flat = api.handle(self.ctx, "archive-list", {"id": job.id})
        self.assertEqual({e["path"] for e in flat["entries"]},
                         {"outer.zip!/a.txt", "outer.zip!/inner.zip"})

    def test_21_api_extract_and_repair(self):
        job, path = self._job("outer.zip", zip_bytes(
            {"a.txt": b"a" * 30, "b.txt": b"b" * 40}))
        dest = self.root / "api-out"
        early = api.handle(self.ctx, "archive-repair",
                           {"id": job.id, "dest": str(dest)})
        self.assertFalse(early["ok"])
        self.assertEqual(early.get("code"), "no_manifest")

        started = api.handle(self.ctx, "archive-extract", {
            "id": job.id, "members": ["outer.zip!/a.txt"], "dest": str(dest)})
        self.assertTrue(started["ok"], started)
        done = self._wait(started["task"])
        self.assertEqual(done["state"], "done", done)
        self.assertEqual(done["result"]["extracted"], 1)
        self.assertEqual((dest / "a.txt").read_bytes(), b"a" * 30)
        self.assertFalse((dest / "b.txt").exists())

        # butun uyeler + onarim
        all_ = api.handle(self.ctx, "archive-extract",
                          {"id": job.id, "members": [], "dest": str(dest)})
        self._wait(all_["task"])
        (dest / "b.txt").write_bytes(b"z")
        repaired = api.handle(self.ctx, "archive-repair",
                              {"id": job.id, "dest": str(dest)})
        self.assertTrue(repaired["ok"], repaired)
        done = self._wait(repaired["task"])
        self.assertEqual(done["state"], "done", done)
        self.assertEqual(done["result"]["extracted"], 1)
        self.assertEqual((dest / "b.txt").read_bytes(), b"b" * 40)

    def test_22_api_tar_extract(self):
        job, _ = self._job("paket.tar.gz", make_tar(
            self.root / "paket.tar.gz", {"x/y.txt": b"y"}).read_bytes())
        dest = self.root / "tar-out"
        res = api.handle(self.ctx, "archive-extract",
                         {"id": job.id, "members": [], "dest": str(dest)})
        self.assertTrue(res["ok"], res)
        done = self._wait(res["task"])
        self.assertEqual(done["state"], "done", done)
        self.assertEqual((dest / "x" / "y.txt").read_bytes(), b"y")

    def test_23_zip_preview_compat(self):
        job, _ = self._job("old.zip", zip_bytes({"a.txt": b"a" * 5}))
        res = api.handle(self.ctx, "zip-preview", {"id": job.id})
        self.assertTrue(res["ok"], res)
        name, size, csize = res["entries"][0]
        self.assertEqual((name, size), ("a.txt", 5))
        self.assertGreaterEqual(csize, 0)
        self.assertEqual(res["note"], "")

    def test_24_api_errors(self):
        job, _ = self._job("bozuk.7z", b"7z bozuk veri")
        res = api.handle(self.ctx, "archive-list", {"id": job.id})
        self.assertFalse(res["ok"])
        self.assertIn("desteklenmeyen", res["error"])
        self.assertFalse(api.handle(self.ctx, "archive-list",
                                    {"id": "yok"})["ok"])
        self.assertFalse(api.handle(self.ctx, "archive-status",
                                    {"task": "yok"})["ok"])
        self.assertFalse(api.handle(self.ctx, "archive-extract", {
            "id": job.id, "members": "bozuk"})["ok"])

    def test_25_api_rejects_relative_dest(self):
        job, _ = self._job("outer.zip", zip_bytes({"a.txt": b"a"}))
        for cmd in ("archive-extract", "archive-repair"):
            res = api.handle(self.ctx, cmd,
                             {"id": job.id, "dest": "goreli/dizin"})
            self.assertFalse(res["ok"], res)
            self.assertIn("mutlak", res["error"])

    def test_26_api_force_passthrough(self):
        job, _ = self._job("outer.zip", zip_bytes({"a.txt": b"arsiv"}))
        dest = self.root / "api-force"
        dest.mkdir()
        (dest / "a.txt").write_bytes(b"kullanici")
        blocked = api.handle(self.ctx, "archive-extract", {
            "id": job.id, "members": [], "dest": str(dest)})
        self.assertTrue(blocked["ok"], blocked)
        done = self._wait(blocked["task"])
        self.assertEqual(done["state"], "error", done)
        self.assertEqual((dest / "a.txt").read_bytes(), b"kullanici")
        forced = api.handle(self.ctx, "archive-extract", {
            "id": job.id, "members": [], "dest": str(dest), "force": True})
        self.assertTrue(forced["ok"], forced)
        done = self._wait(forced["task"])
        self.assertEqual(done["state"], "done", done)
        self.assertEqual((dest / "a.txt").read_bytes(), b"arsiv")


class CliArchiveTests(unittest.TestCase):
    """CLI goreli --dest gonderirse daemon'un CWD'sine bagli kalirdi."""

    class _Fake:
        def __init__(self):
            self.sent = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, cmd, **params):
            self.sent.append((cmd, params))
            if cmd == "archive-extract":
                return {"ok": True, "task": "t1", "dest": "/x",
                        "state": "running"}
            return {"ok": True, "state": "done",
                    "result": {"ok": True, "extracted": 1, "dest": "/x",
                               "errors": [], "repair": False}}

    def test_27_cli_sends_absolute_dest(self):
        fake = self._Fake()
        args = argparse.Namespace(action="extract", id="j1", members=[],
                                  dest="goreli/alt", force=True)
        with mock.patch.object(cli, "_client",
                               lambda env, spawn=True: fake), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = cli.cmd_archive(args, None)
        self.assertEqual(rc, 0)
        cmd, params = fake.sent[0]
        self.assertEqual(cmd, "archive-extract")
        self.assertEqual(params["dest"], os.path.abspath("goreli/alt"))
        self.assertTrue(params["force"])

    def test_28_cli_repair_absolutises_dest(self):
        fake = self._Fake()
        args = argparse.Namespace(action="repair", id="j1", dest="alt",
                                  force=False)
        with mock.patch.object(cli, "_client",
                               lambda env, spawn=True: fake), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = cli.cmd_archive(args, None)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.sent[0][1]["dest"], os.path.abspath("alt"))
        self.assertFalse(fake.sent[0][1]["force"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
