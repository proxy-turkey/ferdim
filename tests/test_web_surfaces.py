"""Web arayuzu yuzeyleri: ana makine profilleri paneli ve dogrulama
rozetleri (1.7). Tarayici gerektirmez; kaynaklari statik denetler.
Canli davranis tests/ui/web_live.py icinde kosar."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


class HostsPanelTests(unittest.TestCase):
    """Ogrenilmis ana makine profilleri paneli (liste / unut / tumunu unut)."""

    @classmethod
    def setUpClass(cls):
        cls.js = (WEB / "app.js").read_text("utf-8")
        cls.html = (WEB / "index.html").read_text("utf-8")
        cls.css = (WEB / "style.css").read_text("utf-8")

    def test_01_panel_markup_and_a11y(self):
        for needle in ('id="hostsBtn"', 'aria-haspopup="dialog"',
                       'id="hostsDlg"', 'aria-labelledby="hostsTitle"',
                       'id="hostsTitle"', 'id="hostsList"', 'role="list"',
                       'id="hostsEmpty"', 'id="hostsStatus"',
                       'aria-live="polite"', 'id="hostsForgetAll"',
                       'id="hostsClose"'):
            self.assertIn(needle, self.html, needle)
        # kapanis dugmesi dialog'u kapatir; panel <dialog> olarak acilir
        self.assertIn('hostsDlg.showModal()', self.js)
        self.assertIn('hostsDlg.close()', self.js)

    def test_02_hosts_command_wiring(self):
        self.assertIn('"/api/hosts"', self.js)
        self.assertIn("forget: host", self.js)      # tek profili unut
        self.assertIn("clear: true", self.js)       # tumunu sil
        # cevrilmis metinler sunucudan gelen anahtarlarla kurulur
        for key in ("hosts_row", "hosts_forgotten", "hosts_unknown",
                    "hosts_cleared", "rtt_"):
            self.assertIn(key, self.js, key)
        self.assertIn('data-i18n="hosts_empty"', self.html)

    def test_03_rows_keyed_by_host_updated_in_place(self):
        self.assertRegex(self.js, r"const hostRows = new Map\(\)")
        self.assertIn("hostRows.set(h.host, r)", self.js)
        self.assertIn("insertBefore(r.root, cursor)", self.js)
        self.assertIn("r.text.textContent = text", self.js)

    def test_04_forget_button_has_accessible_name(self):
        # her satirin unut dugmesi: ana makineyi iceren aria-label
        self.assertRegex(self.js, r'aria-label"\) !== label')
        self.assertRegex(self.js, r'tr\("act_remove"\) \+ ": " \+ h\.host')
        self.assertIn('"data-host": host', self.js)

    def test_05_no_markup_injection(self):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                     "document.write"):
            self.assertNotIn(sink, self.js, sink)
        # satir ici olay niteligi yok (dinleyiciler JS'te baglanir)
        self.assertNotRegex(self.html, r"\son[a-z]+=")

    def test_06_every_id_used_by_app_exists(self):
        used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', self.js))
        missing = sorted(i for i in used if f'id="{i}"' not in self.html)
        self.assertEqual(missing, [], "app.js olmayan kimlige baglaniyor")


class IntegrityBadgeTests(unittest.TestCase):
    """Is satirindaki saglama / Merkle rozetleri (ok / warn / mismatch)."""

    @classmethod
    def setUpClass(cls):
        cls.js = (WEB / "app.js").read_text("utf-8")
        cls.css = (WEB / "style.css").read_text("utf-8")

    def test_01_badge_states_use_existing_strings(self):
        for key in ("chk_ok", "chk_warn", "notify_chk_mismatch",
                    "err_checksum_mismatch", "notify_chk_ok", "merkle_ok",
                    "merkle_manifest", "err_merkle_mismatch"):
            self.assertIn(key, self.js, key)

    def test_02_badge_reads_snapshot_fields(self):
        for field in ("j.checksum", "j.merkle", "j.verified",
                      "checksum_note", "merkle_note"):
            self.assertIn(field, self.js, field)
        self.assertEqual(self.js.count("function integrityNotes"), 1)

    def test_03_note_carries_tooltip(self):
        # rozet ipucu: not basligi title niteligiyle tasinir
        self.assertIn("dom(\"span\", { class: \"note \" + cls, text, title })",
                      self.js)

    def test_04_badge_style_is_compact(self):
        self.assertIn(".note.chk", self.css)
        self.assertIn("border:1px solid currentColor", self.css)


if __name__ == "__main__":
    unittest.main(verbosity=2)
