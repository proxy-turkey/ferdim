"""Yayin yardimcisi (`ferdim publish`): uretilen .ferdim-merkle / .sha256 /
.meta4 dosyalari.

Sozlesme:
  * .sha256 coreutils bicimindedir ("ozet  ad"), .meta4 RFC 5854'tur ve
    FerDim'in kendi metalink okuyucusundan gecmelidir (yuvarlak yol).
  * Cikti BYT-KARARLI: ayni girdiyle ikinci calistirma hicbir dosyayi
    degistirmez (mtime dahil), yalnizca gercekten bayat olan bildirim
    yeniden uretilir.
  * Kendi ciktilarimiz (merkle/sha256/meta4, .part) yayinlanacak dosya
    sayilmaz; boylece ikinci calistirma kendini besleyip sismez.
"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import cdc, cli, metalink, publish
from tests.helpers import tmp_root

BASE = "https://dl.example.org/rel"


def _plain(el) -> str:
    """ET, xmlns bildirimini gercek ad alanina cevirir: etiketi sadelestir."""
    t = el.tag
    if isinstance(t, str) and t.startswith("{"):
        return t.split("}", 1)[1]
    return t


def _kids(parent, name):
    return [e for e in parent.iter() if _plain(e) == name]


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.root = tmp_root(self, "ferdim-publish-")
        self.dir = self.root / "site"
        self.dir.mkdir()
        self.a = self._file("alpha.iso", b"alpha" * 10_000)
        self.b = self._file("beta file.zip", b"beta" * 100)

    def _file(self, name: str, data: bytes) -> Path:
        p = self.dir / name
        p.write_bytes(data)
        os.utime(p, (1_700_000_000, 1_700_000_000))
        return p

    def _run(self, **kw):
        opts = {"base_url": BASE}
        opts.update(kw)
        return publish.publish(self.dir, **opts)

    # ---------------------------------------------------------- yan dosyalar
    def test_outputs_written_next_to_artefact(self):
        res = self._run()
        self.assertEqual([f["name"] for f in res["files"]],
                         ["alpha.iso", "beta file.zip"])
        for name in ("alpha.iso", "beta file.zip"):
            for suffix in (cdc.SUFFIX, publish.SHA256_SUFFIX,
                           publish.META4_SUFFIX):
                self.assertTrue((self.dir / (name + suffix)).is_file(),
                                name + suffix)
        self.assertTrue((self.dir / publish.INDEX_NAME).is_file())
        self.assertEqual(Path(res["index"]),
                         self.dir / publish.INDEX_NAME)

    def test_sha256_sidecar_is_coreutils_format(self):
        self._run()
        text = (self.dir / "alpha.iso.sha256").read_text("utf-8")
        self.assertEqual(text.count("\n"), 1, "tek satir, sonu yeni satir")
        digest, name = text.rstrip("\n").split("  ", 1)
        self.assertEqual(name, "alpha.iso")
        self.assertEqual(digest,
                         hashlib.sha256(self.a.read_bytes()).hexdigest())

    # --------------------------------------------------------------- meta4
    def test_meta4_files_are_direct_children_of_metalink(self):
        """RFC 5854 (4.1.1): <file> ogeleri dogrudan <metalink> altindadir;
        <files> kapsayicisi Metalink 3.0'a aittir, RFC 5854'te yoktur."""
        self._run()
        root = ET.parse(self.dir / "index.meta4").getroot()
        self.assertEqual(_plain(root), "metalink")
        self.assertEqual([_plain(e) for e in root],
                         ["published", "file", "file"])
        self.assertNotIn("files", {_plain(e) for e in root.iter()})

    def test_meta4_round_trips_through_metalink_parser(self):
        self._run(mirrors=["https://mirror.example.net/f",
                           "https://m2.example.net"])
        files = metalink.parse(
            (self.dir / "index.meta4").read_text("utf-8"))
        self.assertEqual([f["filename"] for f in files],
                         ["alpha.iso", "beta file.zip"])
        alpha = files[0]
        self.assertEqual(alpha["size"], len(self.a.read_bytes()))
        self.assertEqual(alpha["sha256"],
                         hashlib.sha256(self.a.read_bytes()).hexdigest())
        # tercih sirasi: birincil adres once, sonra aynalar
        self.assertEqual(alpha["urls"], [
            BASE + "/alpha.iso",
            "https://mirror.example.net/f/alpha.iso",
            "https://m2.example.net/alpha.iso",
        ])
        self.assertEqual(files[1]["urls"][0],
                         BASE + "/beta%20file.zip",
                         "bosluk yuzde-kodlanmali")
        # tek dosyalik belge de ayni yuvarlak yolu gecmeli
        one = metalink.parse(
            (self.dir / "alpha.iso.meta4").read_text("utf-8"))
        self.assertEqual([f["filename"] for f in one], ["alpha.iso"])
        self.assertEqual(one[0]["size"], alpha["size"])

    def test_meta4_carries_metaurl_and_merkle_root(self):
        self._run()
        root = ET.parse(self.dir / "alpha.iso.meta4").getroot()
        file_el = _kids(root, "file")[0]

        def els(tag):
            return [e for e in file_el if _plain(e) == tag]

        metaurl = els("metaurl")
        self.assertEqual(len(metaurl), 1)
        self.assertEqual(metaurl[0].get("mediatype"),
                         "application/x-ferdim-merkle")
        self.assertEqual(metaurl[0].text, BASE + "/alpha.iso.ferdim-merkle")
        roots = [h.text for h in els("hash")
                 if h.get("type") == "ferdim-merkle-root"]
        self.assertEqual(len(roots), 1)
        man = cdc.read_manifest(self.dir / ("alpha.iso" + cdc.SUFFIX))
        self.assertEqual(roots[0], man.root)
        self.assertEqual(man.size, self.a.stat().st_size)

    def test_published_is_mtime_in_rfc3339_utc(self):
        self._run()
        root = ET.parse(self.dir / "alpha.iso.meta4").getroot()
        published = _kids(root, "published")[0].text
        self.assertEqual(published, "2023-11-14T22:13:20Z")
        self.assertEqual(self.a.stat().st_mtime, 1_700_000_000)

    # ------------------------------------------------------- bayt-kararlilik
    def test_second_run_changes_nothing(self):
        self._run()
        snap = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.dir.iterdir()}
        self._run()
        again = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.dir.iterdir()}
        self.assertEqual(sorted(snap), sorted(again))
        for name in snap:
            self.assertEqual(snap[name][0], again[name][0], f"icerik: {name}")
            self.assertEqual(snap[name][1], again[name][1], f"mtime: {name}")

    def test_changed_artefact_regenerates_manifest(self):
        self._run()
        old = cdc.read_manifest(self.dir / ("alpha.iso" + cdc.SUFFIX))
        self._file("alpha.iso", b"changed" * 20_000)
        res = self._run()
        new = cdc.read_manifest(self.dir / ("alpha.iso" + cdc.SUFFIX))
        self.assertNotEqual(old.root, new.root)
        self.assertEqual(new.size, self.a.stat().st_size)
        self.assertEqual(res["files"][0]["merkle_root"], new.root)
        sha = (self.dir / "alpha.iso.sha256").read_text("utf-8").split("  ")[0]
        self.assertEqual(sha,
                         hashlib.sha256(self.a.read_bytes()).hexdigest())

    def test_corrupt_manifest_is_rebuilt(self):
        man = self.dir / ("alpha.iso" + cdc.SUFFIX)
        man.write_text('{"v":1}', "utf-8")
        self._run()
        self.assertEqual(cdc.read_manifest(man).size,
                         self.a.stat().st_size)

    def test_stale_manifest_is_rebuilt(self):
        self._run()
        man = self.dir / ("alpha.iso" + cdc.SUFFIX)
        os.utime(man, ns=(1_600_000_000_000_000_000,
                          1_600_000_000_000_000_000))
        self._run()
        self.assertGreater(man.stat().st_mtime, 1_700_000_000,
                           "bayat bildirim yeniden uretildi")

    def test_force_rebuilds_up_to_date_manifest(self):
        self._run()
        man = self.dir / ("alpha.iso" + cdc.SUFFIX)
        # bildirimi "guncel" birak ama mtime'i taniy: yazilirsa degisir
        os.utime(man, ns=(1_700_000_000_000_000_000,
                          1_700_000_000_000_000_000))
        self._run(force=True)
        self.assertGreater(man.stat().st_mtime, 1_700_000_000,
                           "--force guncel bildirimi de yeniden uretir")

    def test_same_size_new_mtime_regenerates_manifest(self):
        """Icerik ayni boyutta degistiginde mtime olcutu yakalar."""
        self._run()
        man = self.dir / ("alpha.iso" + cdc.SUFFIX)
        old = cdc.read_manifest(man)
        data = self.a.read_bytes()[::-1]           # ayni boyut, farkli icerik
        self.a.write_bytes(data)                   # mtime = simdi
        self._run()
        new = cdc.read_manifest(man)
        self.assertNotEqual(old.root, new.root)
        self.assertEqual((self.dir / "alpha.iso.sha256").read_text(
            "utf-8").split("  ")[0], hashlib.sha256(data).hexdigest())

    def test_own_outputs_are_not_published(self):
        self._run()
        files = metalink.parse((self.dir / "index.meta4").read_text("utf-8"))
        self.assertEqual([f["filename"] for f in files],
                         ["alpha.iso", "beta file.zip"])

    # ------------------------------------------------------------- secenekler
    def test_glob_limits_the_set(self):
        res = self._run(pattern="*.iso")
        self.assertEqual([f["name"] for f in res["files"]], ["alpha.iso"])
        files = metalink.parse((self.dir / "index.meta4").read_text("utf-8"))
        self.assertEqual([f["filename"] for f in files], ["alpha.iso"])
        self.assertFalse((self.dir / "beta file.zip.sha256").exists())

    def test_no_merkle_skips_manifest_and_metaurl(self):
        res = self._run(merkle=False)
        self.assertEqual([p.name for p in self.dir.iterdir()
                          if p.name.endswith(cdc.SUFFIX)], [])
        root = ET.parse(self.dir / "alpha.iso.meta4").getroot()
        self.assertEqual(_kids(root, "metaurl"), [])
        self.assertEqual([e for e in _kids(root, "hash")
                          if e.get("type") == "ferdim-merkle-root"], [])
        self.assertEqual(res["files"][0]["merkle_path"], "")
        # sha256 + meta4 hala uretilir
        self.assertTrue((self.dir / "alpha.iso.sha256").is_file())

    def test_rejects_non_http_base_and_mirror(self):
        with self.assertRaises(ValueError):
            self._run(base_url="ftp://example.org/rel")
        with self.assertRaises(ValueError):
            self._run(mirrors=["file:///srv/mirror"])

    def test_empty_directory_is_an_error(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError):
            publish.publish(empty, base_url=BASE)

    def test_missing_directory_is_an_error(self):
        with self.assertRaises(ValueError):
            publish.publish(self.root / "nope", base_url=BASE)


class PublishCliTest(unittest.TestCase):
    def setUp(self):
        self.root = tmp_root(self, "ferdim-publish-cli-")
        self.dir = self.root / "site"
        self.dir.mkdir()
        (self.dir / "one.bin").write_bytes(b"x" * 1000)
        (self.dir / "two.bin").write_bytes(b"y" * 2000)

    def test_parser_wiring(self):
        args = cli.build_parser().parse_args(
            ["publish", str(self.dir), "--base-url", BASE,
             "--mirror", "https://m.example.net", "--glob", "*.bin",
             "--no-merkle", "--force"])
        self.assertIs(args.fn, cli.cmd_publish)
        self.assertEqual(args.dir, str(self.dir))
        self.assertEqual(args.base_url, BASE)
        self.assertEqual(args.mirror, ["https://m.example.net"])
        self.assertEqual(args.glob, "*.bin")
        self.assertTrue(args.no_merkle)
        self.assertTrue(args.force)

    def test_cmd_publish_prints_listing(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_publish(
                cli.build_parser().parse_args(
                    ["publish", str(self.dir), "--base-url", BASE]), None)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        for name in ("one.bin", "two.bin", publish.INDEX_NAME):
            self.assertIn(name, out)
        self.assertIn("one.bin.sha256", out)
        self.assertTrue((self.dir / publish.INDEX_NAME).is_file())

    def test_cmd_publish_reports_errors(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", err):
            rc = cli.cmd_publish(
                cli.build_parser().parse_args(
                    ["publish", str(self.root / "nope"),
                     "--base-url", BASE]), None)
        self.assertEqual(rc, 1)
        self.assertIn("dizin yok", err.getvalue())


if __name__ == "__main__":
    unittest.main()
