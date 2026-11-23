"""GTK arayuzu statik kurallari.

Kullanimi onerilmeyen dialog sinifi artik cagrilmamali (modal pencereler
kullanilir) ve ayar penceresinin yeni dizgileri dort dilde de bulunmali.
"""
import unittest
from pathlib import Path

from ferdim.i18n import LANGS, S

ROOT = Path(__file__).resolve().parents[1]
GUI_DIR = ROOT / "ferdim" / "gui"
# ayar penceresi / modal pencere ile gelen yeni anahtarlar
NEW_KEYS = ("lang_auto", "err_time", "lang_restart", "lbl_clipboard",
            "save_failed")


class GuiStaticTest(unittest.TestCase):
    def test_01_no_deprecated_gtk_dialog(self):
        for path in sorted(GUI_DIR.glob("*.py")):
            src = path.read_text("utf-8")
            self.assertNotIn("Gtk.Dialog", src, path.name)

    def test_02_new_keys_in_all_languages(self):
        for key in NEW_KEYS:
            self.assertIn(key, S, key)
            self.assertEqual(set(S[key]), set(LANGS), key)

    def test_03_modal_windows_exist(self):
        src = (GUI_DIR / "app.py").read_text("utf-8")
        for name in ("class AddWindow(Gtk.Window)",
                     "class SettingsWindow(Gtk.Window)"):
            self.assertIn(name, src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
