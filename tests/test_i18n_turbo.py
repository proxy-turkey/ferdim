"""i18n tutarliligi ve turbo modu testleri."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim.engine.queue import QueueManager
from ferdim.i18n import LANGS, S, detect_lang, dict_for, t
from ferdim.paths import AppEnv


class I18nTests(unittest.TestCase):
    def test_all_locales_have_same_keys(self):
        for key, entry in S.items():
            for lang in LANGS:
                self.assertIn(lang, entry, f"{key} icinde {lang} eksik")
                self.assertTrue(entry[lang].strip(), f"{key}/{lang} bos")

    def test_dict_for_and_fallback(self):
        for lang in LANGS:
            d = dict_for(lang)
            self.assertEqual(set(d), set(S))
        d = dict_for("xx")  # gecersiz dil -> tr
        self.assertEqual(d["st_done"], S["st_done"]["tr"])

    def test_detect_lang(self):
        self.assertEqual(detect_lang("ru"), "ru")
        self.assertEqual(detect_lang("en_US"), "en")
        self.assertEqual(detect_lang("auto"), detect_lang(None))
        self.assertIn(detect_lang(None), LANGS)

    def test_t_format(self):
        self.assertIn("32", t("turbo_hint", "tr"))


class TurboTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ferdim-turbo-"))
        self.env = AppEnv(root=self.root)
        self.cfg = self.env.load_config()
        # varsayilan artik azami guc; bu test ac/kapa mantigini izole eder
        self.cfg["turbo_mode"] = False
        self.cfg["speed_limit"] = 1_000_000
        self.cfg["max_concurrent"] = 2
        self.qm = QueueManager(self.env, self.cfg)

    def tearDown(self):
        self.qm.shutdown()

    def test_turbo_roundtrip(self):
        self.assertFalse(self.cfg.get("turbo_mode"))
        self.qm.set_turbo(True)
        self.assertTrue(self.cfg["turbo_mode"])
        self.assertEqual(self.cfg["speed_limit"], 0)          # limitsiz
        self.assertGreaterEqual(self.cfg["max_concurrent"], 8)  # sisirildi
        self.assertEqual(self.qm.bucket.rate, 0)
        st = self.qm.stats()
        self.assertTrue(st["turbo"])

        self.qm.set_turbo(False)
        self.assertFalse(self.cfg["turbo_mode"])
        self.assertEqual(self.cfg["speed_limit"], 1_000_000)   # geri yuklendi
        self.assertEqual(self.cfg["max_concurrent"], 2)
        self.assertEqual(self.qm.bucket.rate, 1_000_000)
        self.assertFalse(self.qm.stats()["turbo"])

    def test_turbo_persisted(self):
        self.qm.set_turbo(True)
        on_disk = self.env.load_config()
        self.assertTrue(on_disk.get("turbo_mode"))
        self.assertEqual(on_disk.get("turbo_prev", {}).get("speed_limit"),
                         1_000_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
