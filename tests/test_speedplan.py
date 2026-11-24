"""Saat bazli hiz plani (ROADMAP 1.8): util penceresi, kuyruk uygulamasi,
manuel ezme ve API/CLI yuzeyi.

Plan kurallari gercek saate gore degerlendirilir; testler ya her gun
gecerli kural ya da bugun OLMAYAN bir gun kullanir (takvime bagimli
olmadan). Governor/kuyruk zamanlamasi icin uyku yok: tick elle cagrilir.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ferdim.api as api
import ferdim.cli as cli
from ferdim import util
from ferdim.engine.queue import QueueManager
from ferdim.paths import DEFAULTS, AppEnv
from tests.helpers import tmp_root

ROOT = Path(__file__).resolve().parent.parent
MB = 1024 ** 2
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ALL_DAY = {"days": "", "from": "", "to": "", "limit": "2MB"}


def fresh_env(case, prefix="ferdim-plan-") -> AppEnv:
    return AppEnv(root=tmp_root(case, prefix))


class DayWindowTests(unittest.TestCase):
    def test_01_parse_days(self):
        self.assertEqual(util.parse_days(""), frozenset(range(7)))
        self.assertEqual(util.parse_days("*"), frozenset(range(7)))
        self.assertEqual(util.parse_days("mon-fri"), frozenset(range(5)))
        self.assertEqual(util.parse_days("sat,sun"), frozenset({5, 6}))
        self.assertEqual(util.parse_days("fri-mon"),
                         frozenset({4, 5, 6, 0}))     # sarma
        self.assertEqual(util.parse_days("WED"), frozenset({2}))
        for bad in ("xyz", "mon-xyz", "mon,,friday?"):
            with self.assertRaises(ValueError, msg=bad):
                util.parse_days(bad)

    def test_02_window_with_days(self):
        wed_noon = _dt.datetime(2026, 5, 6, 12, 0)      # carsamba
        self.assertTrue(util.window_open("09:00", "18:00", wed_noon,
                                         util.parse_days("mon-fri")))
        self.assertFalse(util.window_open("09:00", "18:00", wed_noon,
                                          util.parse_days("sat-sun")))
        # gece yarisini asan pencere: gun BASLANGIC gunudur
        fri_late = _dt.datetime(2026, 5, 8, 23, 30)     # cuma
        sat_early = _dt.datetime(2026, 5, 9, 1, 0)      # cumartesi
        sat_late = _dt.datetime(2026, 5, 9, 23, 30)
        fri = util.parse_days("fri")
        monday = util.parse_days("mon")
        self.assertTrue(util.window_open("22:00", "02:00", fri_late, fri))
        self.assertTrue(util.window_open("22:00", "02:00", sat_early, fri))
        self.assertFalse(util.window_open("22:00", "02:00", sat_early, monday))
        self.assertFalse(util.window_open("22:00", "02:00", sat_late, fri))
        # bos gun kumesi: hicbir zaman
        self.assertFalse(util.window_open("00:00", "23:59", wed_noon,
                                          frozenset()))

    def test_03_plan_limit_and_first_match(self):
        noon = _dt.datetime(2026, 5, 6, 12, 0)
        plan = [{"days": "", "from": "", "to": "", "limit": "1MB"},
                {"days": "", "from": "", "to": "", "limit": "5MB"}]
        self.assertEqual(util.speed_plan_limit(plan, noon), MB)   # ilk kazanir
        self.assertIsNone(util.speed_plan_limit([]), "plan yok")
        self.assertIsNone(util.speed_plan_limit([], noon))
        self.assertIsNone(util.speed_plan_limit("bozuk", noon))
        night = [{"days": "", "from": "09:00", "to": "18:00", "limit": "2MB"}]
        self.assertEqual(util.speed_plan_limit(night, noon), 2 * MB)
        self.assertEqual(util.speed_plan_limit(
            night, _dt.datetime(2026, 5, 6, 3, 0)), 0)     # eslesme yok: guc
        self.assertEqual(util.speed_plan_limit(
            [{"days": "mon", "limit": "0"}],
            _dt.datetime(2026, 5, 4, 12, 0)), 0)           # "0" = sinirsiz
        self.assertEqual(util.speed_plan_limit(
            [{"days": "", "limit": ""}], noon), 0)

    def test_04_sanitize_plan(self):
        rows = util.sanitize_speed_plan([
            {"days": "mon-fri", "from": "09:00", "to": "18:00", "limit": "2MB"},
            {"days": "", "limit": "512kb"},
            {"days": "xyz", "limit": "1MB"},          # bozuk gun: atilir
            {"days": "", "from": "25:00", "to": "18:00", "limit": "1MB"},
            {"days": "", "from": "09:00", "to": "18:00", "limit": "1MB"},
            {"days": "", "from": "", "to": "", "limit": ""},   # sinirsiz
            {"days": "", "limit": "hizli"},           # bozuk limit: atilir
            {"days": "", "limit": "abc"},             # ayni: atilir
            {"days": "sat", "from": "01:00", "limit": "1MB"},   # eksik saat
            "metin", 7, None])
        self.assertEqual(rows, [
            {"days": "mon-fri", "from": "09:00", "to": "18:00", "limit": "2MB"},
            {"days": "", "from": "", "to": "", "limit": "512kb"},
            {"days": "", "from": "09:00", "to": "18:00", "limit": "1MB"},
            {"days": "", "from": "", "to": "", "limit": "0"},
        ])
        self.assertEqual(util.sanitize_speed_plan(None), [])
        self.assertEqual(util.sanitize_speed_plan({"days": "mon"}), [])


class QueuePlanTests(unittest.TestCase):
    def test_01_plan_applied_and_cleared(self):
        env = fresh_env(self)
        cfg = env.load_config()
        cfg["speed_plan"] = [dict(ALL_DAY)]
        self.assertTrue(cfg["turbo_mode"])
        qm = QueueManager(env, cfg)
        try:
            qm._tick()
            self.assertEqual(qm.cfg["speed_limit"], 2 * MB)
            self.assertEqual(qm.bucket.rate, 2 * MB)
            # turbo "limitsiz" demek: plan limiti turbo modunu birakir
            self.assertFalse(qm.cfg["turbo_mode"])
            st = qm.stats()["speed_plan"]
            self.assertTrue(st["enabled"])
            self.assertFalse(st["manual"])
            self.assertEqual(st["limit"], 2 * MB)
            self.assertEqual(st["active_rule"]["index"], 0)
            self.assertEqual(st["active_rule"]["limit"], 2 * MB)
            # plan silinince plandan onceki kullanici limiti geri gelir
            qm.set_speed_plan([])
            self.assertEqual(qm.cfg["speed_limit"], 0)
            self.assertIsNone(qm.stats()["speed_plan"]["active_rule"])
        finally:
            qm.shutdown()

    def test_02_plan_matches_only_its_window(self):
        env = fresh_env(self)
        cfg = env.load_config()
        today = _dt.datetime.now().weekday()
        other = DAY_NAMES[(today + 3) % 7]
        cfg["speed_plan"] = [{"days": other, "limit": "3MB"}]
        cfg["turbo_mode"] = False
        cfg["speed_limit"] = 500_000
        qm = QueueManager(env, cfg)
        try:
            qm._tick()
            # bugun kural yok: plan sinir koymaz (azami guc), kullanici
            # limiti plandan onceki deger olarak saklanir
            self.assertEqual(qm.cfg["speed_limit"], 0)
            self.assertEqual(qm._plan_saved, 500_000)
            self.assertTrue(qm.stats()["speed_plan"]["enabled"])
            self.assertIsNone(qm.stats()["speed_plan"]["active_rule"])
            # ayni plan "her gun"e cevrilince tick hemen uygular
            qm.set_speed_plan([{"days": "", "limit": "3MB"}])
            self.assertEqual(qm.cfg["speed_limit"], 3 * MB)
            # plan silinince en basta yururlukte olan limit geri gelir
            qm.set_speed_plan([])
            self.assertEqual(qm.cfg["speed_limit"], 500_000)
        finally:
            qm.shutdown()

    def test_03_plan_persisted_and_sanitized(self):
        env = fresh_env(self)
        cfg = env.load_config()
        qm = QueueManager(env, cfg)
        try:
            qm.set_speed_plan([{"days": "mon-fri", "from": "09:00",
                                "to": "18:00", "limit": "2MB"},
                               {"days": "bozuk"}])
            on_disk = env.load_config()["speed_plan"]
            self.assertEqual(len(on_disk), 1)
            self.assertEqual(on_disk[0]["limit"], "2MB")
        finally:
            qm.shutdown()


class PlanApiTests(unittest.TestCase):
    def setUp(self):
        self.env = fresh_env(self)
        self.cfg = self.env.load_config()
        self.qm = QueueManager(self.env, self.cfg)
        self.ctx = {"qm": self.qm, "env": self.env, "cfg": self.cfg,
                    "set_cfg": self.env.save_config}

    def tearDown(self):
        self.qm.shutdown()

    def test_01_speedplan_set_show_clear(self):
        r = api.handle(self.ctx, "speedplan", {"action": "set", "plan": [
            {"days": "mon-fri", "from": "09:00", "to": "18:00",
             "limit": "2MB"},
            {"days": "xyz"}, "cop"]})       # bozuklar atilir
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["plan"]), 1)
        self.assertEqual(r["plan"][0]["limit"], "2MB")
        self.assertEqual(self.env.load_config()["speed_plan"], r["plan"])
        shown = api.handle(self.ctx, "speedplan", {})
        self.assertTrue(shown["ok"])
        self.assertEqual(shown["plan"], r["plan"])
        cleared = api.handle(self.ctx, "speedplan", {"action": "clear"})
        self.assertEqual(cleared["plan"], [])
        self.assertFalse(cleared["enabled"])

    def test_02_manual_limit_wins_until_resume(self):
        self.ctx["cfg"]["speed_plan"] = [dict(ALL_DAY)]
        self.qm._tick()
        self.assertEqual(self.qm.cfg["speed_limit"], 2 * MB)
        r = api.handle(self.ctx, "limit", {"value": "1MB"})
        self.assertEqual(r["speed_limit"], MB)
        self.assertTrue(self.cfg["speed_plan_manual"])
        self.qm._tick()                     # tick elle verileni ezmez
        self.assertEqual(self.qm.cfg["speed_limit"], MB)
        self.assertTrue(self.qm.stats()["speed_plan"]["manual"])
        # plana donus
        r = api.handle(self.ctx, "limit", {"value": "plan"})
        self.assertFalse(self.cfg["speed_plan_manual"])
        self.assertEqual(self.qm.cfg["speed_limit"], 2 * MB)
        self.assertFalse(r["speed_plan"]["manual"])
        # 'speedplan resume' ayni isi yapar
        api.handle(self.ctx, "limit", {"value": 0})
        self.assertTrue(self.cfg["speed_plan_manual"])
        resumed = api.handle(self.ctx, "speedplan", {"action": "resume"})
        self.assertFalse(resumed["manual"])
        self.assertEqual(self.qm.cfg["speed_limit"], 2 * MB)

    def test_03_config_set_speed_plan(self):
        r = api.handle(self.ctx, "config-set", {"config": {
            "speed_plan": [{"days": "", "limit": "4MB"}],
            "speed_plan_manual": "false"}})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["config"]["speed_plan"][0]["limit"], "4MB")
        self.assertIs(r["config"]["speed_plan_manual"], False)
        self.assertEqual(self.qm.cfg["speed_limit"], 4 * MB)

    def test_04_sanitize_config_rejects_junk(self):
        cfg = self.env.load_config()
        cfg["speed_plan"] = [{"days": "xyz", "limit": "1MB"},
                             {"days": "mon", "limit": "1GB"},
                             "junk"]
        cfg["speed_plan_manual"] = "off"
        api._sanitize_config(cfg)
        self.assertEqual([r["days"] for r in cfg["speed_plan"]], ["mon"])
        self.assertIs(cfg["speed_plan_manual"], False)
        cfg["speed_plan_manual"] = "evet"
        api._sanitize_config(cfg)
        self.assertIs(cfg["speed_plan_manual"], True)

    def test_05_config_set_clear_restores_the_user_limit(self):
        """Web ayarlari plani bos liste ile gonderir (config-set): plan
        limiti kalici uygulanmis kalmamali, onceki kullanici limiti
        (diske de) geri gelmeli."""
        self.cfg["turbo_mode"] = False
        self.cfg["speed_limit"] = 500_000
        r = api.handle(self.ctx, "config-set", {"config": {
            "speed_plan": [{"days": "", "limit": "2MB"}]}})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.qm.cfg["speed_limit"], 2 * MB)
        r = api.handle(self.ctx, "config-set", {"config": {"speed_plan": []}})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.qm.cfg["speed_limit"], 500_000)
        self.assertEqual(self.qm.bucket.rate, 500_000)
        st = self.qm.stats()["speed_plan"]
        self.assertFalse(st["enabled"])
        self.assertIsNone(st["active_rule"])
        on_disk = self.env.load_config()
        self.assertEqual(on_disk["speed_plan"], [])
        self.assertEqual(on_disk["speed_limit"], 500_000)   # duzeltilmis deger

    def test_06_plan_emptied_by_any_path_restores(self):
        """Plan bosaltma yalniz set_speed_plan'dan gecmek zorunda degil:
        dogrudan atamadan sonraki tick de limite takili kalmaz."""
        self.cfg["turbo_mode"] = False
        self.cfg["speed_limit"] = 0
        self.cfg["speed_plan"] = [dict(ALL_DAY)]
        self.qm._tick()
        self.assertEqual(self.qm.cfg["speed_limit"], 2 * MB)
        self.cfg["speed_plan"] = []
        self.qm._tick()
        self.assertEqual(self.qm.cfg["speed_limit"], 0)
        self.assertEqual(self.qm.bucket.rate, 0)

    def test_07_manual_limit_during_plan_survives_clear(self):
        """Plan etkinken elle verilen limit 'clear' ile geri gelir: bayat
        plan-oncesi deger degil, kullanicinin son secimi korunur."""
        self.ctx["cfg"]["speed_plan"] = [dict(ALL_DAY)]
        self.qm._tick()
        self.assertEqual(self.qm.cfg["speed_limit"], 2 * MB)
        api.handle(self.ctx, "limit", {"value": "1MB"})
        self.assertEqual(self.qm._plan_saved, MB)
        cleared = api.handle(self.ctx, "speedplan", {"action": "clear"})
        self.assertEqual(cleared["plan"], [])
        self.assertEqual(cleared["limit"], 0)
        self.assertEqual(self.qm.cfg["speed_limit"], MB)
        self.assertEqual(self.qm.bucket.rate, MB)


class PlanCliTests(unittest.TestCase):
    def test_01_parser_surface(self):
        p = cli.build_parser()
        a = p.parse_args(["speedplan"])
        self.assertIs(a.fn, cli.cmd_speedplan)
        self.assertEqual(a.action, "show")
        a = p.parse_args(["speedplan", "set", '[{"days":"","limit":"1MB"}]'])
        self.assertEqual(a.action, "set")
        self.assertEqual(json.loads(a.json_plan)[0]["limit"], "1MB")
        for act in ("clear", "resume"):
            self.assertEqual(p.parse_args(["speedplan", act]).action, act)
        self.assertIs(p.parse_args(["metrics"]).fn, cli.cmd_metrics)

    def test_02_rule_text(self):
        self.assertIn("mon-fri", cli._plan_rule_text(
            {"days": "mon-fri", "from": "09:00", "to": "18:00",
             "limit": "2MB"}))
        self.assertIn("MB", cli._plan_limit_text(2 * MB))


class PlanWebStaticTests(unittest.TestCase):
    """Ayarlar penceresi: okunabilir satirlar + JSON duzenleyici (textContent)."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "web" / "app.js").read_text("utf-8")
        cls.html = (ROOT / "web" / "index.html").read_text("utf-8")

    def test_01_markup(self):
        for needle in ('id="planRows"', 'id="planState"', 'id="setPlan"',
                       'id="planError"', 'data-i18n="plan_title"',
                       'data-i18n="plan_json"'):
            self.assertIn(needle, self.html, needle)

    def test_02_wiring(self):
        self.assertIn("buildPlanRows(cfg.speed_plan)", self.js)
        self.assertIn("speed_plan: plan", self.js)
        self.assertIn('tr("plan_bad")', self.js)
        self.assertIn("plan_state_manual", self.js)
        self.assertNotIn("innerHTML", self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
