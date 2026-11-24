"""GTK 1.7: bicimlendirme birimleri ve pencere modullerinin statik kurallari.

present.py Gtk import etmez; rozet/gecmis/profil satirlari burada gi
olmadan denenir. Pencere davranisi tests/ui/gtk_smoke.py icinde (xvfb +
izole daemon) kosar; burada yalniz kaynak duzeyi ve metinler denetlenir.
gi yoksa testler atlanir (arayuzsuz kurulumda da suite yesil kalir).
"""
import unittest
from pathlib import Path

from ferdim.i18n import LANGS, S

ROOT = Path(__file__).resolve().parents[1]
GUI_DIR = ROOT / "ferdim" / "gui"
GUI_17_KEYS = (
    "hosts_title", "hosts_forget", "hosts_forget_all",
    "hosts_confirm_clear", "chk_merkle_warn")
try:
    from ferdim.gui import present
except (ImportError, SystemExit, OSError) as _e:     # pragma: no cover
    present = None
    _IMPORT_ERROR = _e


class _Base(unittest.TestCase):
    def setUp(self):
        if present is None:                          # pragma: no cover
            self.skipTest(f"GTK yok: {_IMPORT_ERROR}")


class PresentTest(_Base):
    def test_01_is_archive(self):
        self.assertTrue(present.is_archive(
            {"state": "done", "filename": "paket.tar.gz"}))
        self.assertTrue(present.is_archive(
            {"state": "done", "filename": "PAKET.ZIP"}))
        self.assertFalse(present.is_archive(
            {"state": "downloading", "filename": "paket.zip"}))
        self.assertFalse(present.is_archive(
            {"state": "done", "filename": "film.mkv"}))
        self.assertFalse(present.is_archive({"state": "done"}))

    def test_02_badge_ok_warn_mismatch(self):
        symbol, tip, css = present.integrity_badge(
            {"checksum": "ok", "verified": ["sha256"]}, "en")
        self.assertEqual(symbol, "✓")
        self.assertEqual(css, "success")
        self.assertIn("sha256", tip)

        symbol, _tip, css = present.integrity_badge({"checksum": "warn"}, "en")
        self.assertEqual((symbol, css), ("⚠", "warning"))

        symbol, tip, css = present.integrity_badge(
            {"checksum": "mismatch"}, "en")
        self.assertEqual((symbol, css), ("✗", "error"))
        self.assertEqual(tip, S["err_checksum_mismatch"]["en"])

        self.assertIsNone(present.integrity_badge({}, "en"))
        self.assertIsNone(present.integrity_badge(
            {"checksum": "", "merkle": ""}, "en"))

    def test_03_badge_merkle_and_worst_state(self):
        symbol, tip, css = present.integrity_badge({"merkle": "ok"}, "en")
        self.assertEqual((symbol, css), ("✓", "success"))
        self.assertEqual(tip, S["merkle_ok"]["en"])

        symbol, tip, _css = present.integrity_badge({"merkle": "warn"}, "en")
        self.assertEqual((symbol, tip),
                         ("⚠", S["chk_merkle_warn"]["en"]))

        # iki kaynak: en agir durum simgeyi belirler, ipucu ikisini de tasir
        symbol, tip, css = present.integrity_badge(
            {"checksum": "ok", "verified": ["md5"], "merkle": "mismatch"},
            "en")
        self.assertEqual((symbol, css), ("✗", "error"))
        self.assertIn("md5", tip)
        self.assertIn(S["err_merkle_mismatch"]["en"], tip)

    def test_04_badge_language(self):
        _s, tip_en, _c = present.integrity_badge({"checksum": "warn"}, "en")
        _s, tip_tr, _c = present.integrity_badge({"checksum": "warn"}, "tr")
        self.assertNotEqual(tip_en, tip_tr)

    def test_05_history_lines(self):
        rec = {"id": "j1", "filename": "a.zip", "url": "http://h/a.zip",
               "host": "h", "size": 1000, "bytes": 900, "duration": 9.0,
               "avg_links": 4.0, "peak_links": 6, "state": "done",
               "finished_at": 1700000000}
        for lang in LANGS:
            meta = present.history_meta(rec, lang)
            links = present.history_links(rec, lang)
            self.assertTrue(meta and links)
            self.assertNotIn("{", meta + links)
            self.assertIn("4.0", links)
            self.assertNotIn("{", present.history_when(rec["finished_at"]))
        self.assertIn("Done", present.history_meta(rec, "en"))
        self.assertTrue(present.history_when(0) == "")

    def test_06_history_stats_and_host_line(self):
        stats = {"count": 3, "bytes": 2048,
                 "hosts": [{"host": "h", "count": 3, "avg_links": 2.5,
                            "avg_speed": 1024}]}
        line = present.history_stats_line(stats, "en")
        self.assertIn("3", line)
        self.assertIn("h", line)
        self.assertNotIn("{", line)
        row = {"host": "h", "links": 4, "rtt_band": "low",
               "per_link_bps": 2048, "samples": 7, "last_seen": 1700000000}
        text = present.host_line(row, "en")
        self.assertIn("h", text)
        self.assertEqual(text.count("?"), 0)     # rtt bandi cevrildi


class StaticTest(unittest.TestCase):
    def test_10_windows_exist_and_help_shortcut(self):
        for name in ("archive_win.py", "history_win.py", "hosts_win.py"):
            src = (GUI_DIR / name).read_text("utf-8")
            self.assertIn("_bind_escape(self)", src, name)
            # agir istekler ana donguyu kilitlememeli: ayri baglanti
            self.assertNotIn("self.app.client.request", src, name)
            self.assertIn("request_async(", src, name)

    def test_11_no_deprecated_dialog(self):
        for path in sorted(GUI_DIR.glob("*.py")):
            self.assertNotIn("Gtk.Dialog", path.read_text("utf-8"),
                             path.name)

    def test_12_accessible_names(self):
        arc = (GUI_DIR / "archive_win.py").read_text("utf-8")
        self.assertIn('_a11y(cb, "LABEL"', arc)        # secim kutusu
        hist = (GUI_DIR / "history_win.py").read_text("utf-8")
        self.assertIn('_mnemonic("hist_search"', hist)
        self.assertIn('_mnemonic("hist_state"', hist)
        hosts = (GUI_DIR / "hosts_win.py").read_text("utf-8")
        self.assertIn('_a11y(self.listbox, "LABEL"', hosts)

    def test_13_widgets_own_language_state(self):
        src = (GUI_DIR / "widgets.py").read_text("utf-8")
        for needle in ("def gtr(", "def set_gui_lang(", "def run_async("):
            self.assertIn(needle, src)


class KeyTest(unittest.TestCase):
    def test_20_new_keys_all_languages(self):
        for key in GUI_17_KEYS:
            self.assertIn(key, S, key)
            self.assertEqual(set(S[key]), set(LANGS), key)

    def test_21_badge_keys_all_languages(self):
        for key in ("chk_ok", "chk_warn", "merkle_ok", "err_checksum_mismatch",
                    "err_merkle_mismatch", "hist_redownloaded",
                    "hosts_row", "hosts_forgotten", "hosts_cleared"):
            self.assertIn(key, S, key)
            self.assertEqual(set(S[key]), set(LANGS), key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
