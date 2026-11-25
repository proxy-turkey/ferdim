"""GNOME Shell eklentisi: EGO inceleme kurallari + paketleme.

Kaynak duzeyinde denetlenen kurallar (gjs.guide review-guidelines):
- modul/yapici tarafinda calisma yok, her sey enable() icinde
- her connect/timeout'un disable() yolunda bir karsiligi var
- kullanilmayan (deprecated) modul yok, Gtk/Gdk/Adw yok
- metadata.json yalnizca gerekli anahtarlari tasir ve surumle eslesir
- zip yalnizca gerekli dosyalari icerir
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ferdim
from tests.helpers import tmp_root

EXT_ROOT = ROOT / "gnome-shell-extension"
UUID = "ferdim@proxy-turkey.github.io"
EXT_DIR = EXT_ROOT / UUID
EXT_JS = EXT_DIR / "extension.js"
META = EXT_DIR / "metadata.json"
PACK_SCRIPT = ROOT / "scripts" / "pack_gnome_extension.sh"
INSTALL_DOC = EXT_ROOT / "INSTALL.md"
WORKFLOW = ROOT / ".github" / "workflows" / "gnome-ext.yml"

META_KEYS = {"uuid", "name", "description", "shell-version", "url",
             "version-name"}
ZIP_FILES = {"metadata.json", "extension.js", "LICENSE"}


def source() -> str:
    return EXT_JS.read_text("utf-8")


def metadata() -> dict:
    return json.loads(META.read_text("utf-8"))


class MetadataTests(unittest.TestCase):
    def test_only_documented_keys(self):
        meta = metadata()
        self.assertEqual(set(meta), META_KEYS)
        # "version" EGO tarafindan atanir; eklenti yazmaz (deprecated).
        self.assertNotIn("version", meta)
        self.assertNotIn("session-modes", meta)

    def test_uuid_matches_directory_and_namespace(self):
        meta = metadata()
        self.assertEqual(meta["uuid"], EXT_DIR.name)
        self.assertRegex(meta["uuid"], r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$")
        self.assertNotEqual(meta["uuid"].split("@", 1)[1], "gnome.org")

    def test_name_description_and_url(self):
        meta = metadata()
        self.assertTrue(meta["name"].strip())
        self.assertGreater(len(meta["description"]), 80)
        self.assertTrue(meta["url"].startswith("https://github.com/"))

    def test_shell_version_is_stable_only(self):
        meta = metadata()
        self.assertEqual(meta["shell-version"], ["47", "48", "49"])
        for entry in meta["shell-version"]:
            self.assertRegex(entry, r"^\d+$")

    def test_version_name_tracks_the_release(self):
        # Surum bump'inda metadata.json version-name de guncellenir.
        name = metadata()["version-name"]
        self.assertEqual(name, ferdim.__version__)
        # EGO bu alani dogrular: harf/rakam/bosluk/nokta, 1-16 karakter.
        self.assertRegex(name, r"^(?!^[. ]+$)[a-zA-Z0-9 .]{1,16}$")


class SourceTests(unittest.TestCase):
    def test_no_deprecated_or_forbidden_modules(self):
        src = source()
        for token in ("imports.gi", "imports.misc", "ByteArray", "Mainloop",
                      "Lang.", "gi://Gtk", "gi://Gdk", "gi://Adw"):
            self.assertNotIn(token, src, token)
        for module in re.findall(r"^import .* from '([^']+)'", src, re.M):
            self.assertRegex(module, r"^(gi://|resource:///)")

    def test_no_work_during_module_initialization(self):
        # Sutun 0 = modul govdesi: yalnizca sabitler ve saf yardimcilar.
        for line in source().splitlines():
            if not line or line[0].isspace() or line.lstrip().startswith(
                    ("//", "/*", "*")):
                continue
            self.assertNotIn(" new ", line, line)
            self.assertNotIn(".connect(", line, line)
            self.assertNotIn("timeout_add", line, line)

    def test_extension_class_has_no_constructor(self):
        src = source()
        cls = re.search(
            r"export default class \w+ extends Extension \{(.*)\n\}\n?$",
            src, re.S)
        self.assertIsNotNone(cls)
        self.assertNotIn("constructor(", cls.group(1))

    def test_every_connection_goes_through_the_tracker(self):
        # Tek defter: dogrudan connect/timeout cagrisi Tracker icinde kalir,
        # boylece disable() hepsini tek yerden birakir.
        src = source()
        allowed = ("object.connect(", "this._tracker.connect(",
                   "tracker.connect(")
        for line in src.splitlines():
            if ".connect(" in line:
                self.assertTrue(any(token in line for token in allowed), line)
        self.assertEqual(src.count("object.connect("), 1)
        self.assertEqual(src.count("object.disconnect("), 1)
        self.assertEqual(src.count("GLib.timeout_add"), 1)
        self.assertEqual(src.count("GLib.Source.remove("), 1)

    def test_disable_releases_everything(self):
        src = source()
        destroy = src.split("    destroy() {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("this._menuTracker?.release()", destroy)
        self.assertIn("this._tracker.release()", destroy)
        self.assertIn("this._proxy = null", destroy)
        self.assertIn("super.destroy()", destroy)

        disable = src.split("    disable() {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("this._indicator?.destroy()", disable)
        self.assertIn("this._indicator = null", disable)

    def test_no_log_spam(self):
        # Yalnizca kopru yoksa tek satir uyari.
        self.assertLessEqual(source().count("console."), 1)

    def test_install_doc_documents_ego_upload(self):
        doc = INSTALL_DOC.read_text("utf-8")
        self.assertIn("https://extensions.gnome.org/upload/", doc)


class SyntaxTests(unittest.TestCase):
    def _copy(self) -> Path:
        root = tmp_root(self, "ferdim-gnome-ext-")
        target = root / "extension.mjs"
        target.write_text(source(), "utf-8")
        return target

    @unittest.skipUnless(shutil.which("node"), "node yok")
    def test_extension_parses_with_node(self):
        proc = subprocess.run([shutil.which("node"), "--check",
                               str(self._copy())],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    @unittest.skipUnless(shutil.which("gjs"), "gjs yok")
    def test_extension_parses_with_gjs(self):
        # gjs -m shell modullerini bulamaz (resource:/// kabuk icinde var);
        # ayristirma hatasi ile "modul yok" hatasini boyle ayiririz.
        proc = subprocess.run([shutil.which("gjs"), "-m", str(self._copy())],
                              capture_output=True, text=True, timeout=60)
        output = proc.stdout + proc.stderr
        self.assertNotIn("SyntaxError", output, output)
        self.assertIn("resource:///", output, output)


class ChangedContractTests(unittest.TestCase):
    """Panel/kopru sozlesmesi: Changed kucuk kare tasir.

    Kopru (ferdim/dbusbridge.py) her watch karesini toplamlar + etkin
    islere indirger; kabuk tarafi ayni alanlari okur ve kendi yoklamasini
    yalniz kurtarma icin tutar. Iki dosya birlikte gonderilir.
    """

    def test_01_extension_reads_compact_fields(self):
        src = source()
        self.assertIn("Number.isInteger(stats.active)", src)
        self.assertIn("stats.total_speed", src)
        self.assertIn("stats.jobs", src)

    def test_02_polling_is_recovery_only(self):
        src = source()
        self.assertIn("RECOVERY_SECONDS", src)
        self.assertNotIn("REFRESH_SECONDS", src)
        # 2 sn'lik tam yoklama kaldirildi: panel Changed ile beslenir
        self.assertNotIn("timeout(REFRESH_SECONDS", src)

    def test_03_bridge_and_extension_share_the_fields(self):
        bridge = (ROOT / "ferdim" / "dbusbridge.py").read_text("utf-8")
        src = source()
        for field in ("total_speed", "active", "jobs"):
            self.assertIn(field, src, field)
            self.assertIn(field, bridge, field)
        self.assertIn("compact_frame", bridge)
        self.assertIn("STATS_JOBS_DEFAULT", bridge)


@unittest.skipUnless(shutil.which("gnome-extensions"),
                     "gnome-extensions yok (gnome-shell paketi)")
class PackTests(unittest.TestCase):
    def test_pack_script_builds_only_expected_files(self):
        out = tmp_root(self, "ferdim-gnome-pack-")
        proc = subprocess.run(["bash", str(PACK_SCRIPT), str(out)],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        zip_path = out / f"{UUID}.shell-extension.zip"
        self.assertTrue(zip_path.is_file())
        with zipfile.ZipFile(zip_path) as zf:
            self.assertEqual(set(zf.namelist()), ZIP_FILES)
            packed = json.loads(zf.read("metadata.json"))
            self.assertEqual(zf.read("extension.js").decode("utf-8"),
                             source())
        self.assertEqual(packed, metadata())
        self.assertEqual(packed["uuid"], UUID)

    def test_pack_script_is_version_locked(self):
        text = PACK_SCRIPT.read_text("utf-8")
        self.assertIn("gnome-extensions pack", text)
        self.assertIn('--extra-source="$SRC/LICENSE"', text)
        self.assertIn("ferdim.__version__", text)


class WorkflowTests(unittest.TestCase):
    def test_tag_workflow_uploads_release_asset(self):
        text = WORKFLOW.read_text("utf-8")
        self.assertIn('tags: ["v*"]', text)
        self.assertIn("contents: write", text)
        self.assertIn("gh release upload", text)
        self.assertIn("scripts/pack_gnome_extension.sh", text)


if __name__ == "__main__":
    unittest.main()
