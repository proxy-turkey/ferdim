"""1.13 performans kapisi - karsilastirma mantigi (hizli test; olcum YOK).

Gercek benchmark'lar saniyeler surer ve makineye bagimlidir; burada
yalnizca saf karsilastirma/normalizasyon fonksiyonlari ve depodaki
baseline dosyasinin tutarliligi dogrulanir (JSON okuma + aritmetik).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks import compare

BASELINE = (Path(__file__).resolve().parent.parent
            / "benchmarks" / "baseline.json")


def _rate(actual, base=100.0, factor=1.0):
    return compare.compare({"engine_mib_s": base},
                           {"engine_mib_s": actual}, factor)[0]


def _absolute(name, actual, base):
    return compare.compare({name: base}, {name: actual}, 1.0, True)[0]


class RateMetricTest(unittest.TestCase):
    """Hiz metrikleri: yuksek iyi, kalibrasyonla olcekli, esik %20."""

    def test_equal_is_ok(self):
        v = _rate(100.0)
        self.assertEqual(v["status"], "ok")
        self.assertAlmostEqual(v["ratio"], 1.0)

    def test_faster_is_ok(self):
        self.assertEqual(_rate(150.0)["status"], "ok")

    def test_exactly_twenty_percent_is_still_ok(self):
        # esik "fazla" gerileme: tam %20 gecer (sinir dahil)
        self.assertEqual(_rate(80.0)["status"], "ok")

    def test_over_twenty_percent_fails(self):
        v = _rate(79.0)
        self.assertEqual(v["status"], "fail")
        self.assertTrue(compare.failed([v]))

    def test_calibration_scales_expectation(self):
        # makine 2x hizli: ayni kod 2x hizli olmali
        v = _rate(200.0, base=100.0, factor=2.0)
        self.assertAlmostEqual(v["target"], 200.0)
        self.assertAlmostEqual(v["limit"], 160.0)   # %80 esigi
        self.assertEqual(v["status"], "ok")

    def test_faster_machine_does_not_hide_regression(self):
        # 2x hizli makinede beklenen 200; 140 = %30 gerileme -> fail
        self.assertEqual(_rate(140.0, base=100.0, factor=2.0)["status"],
                         "fail")


class AbsoluteMetricTest(unittest.TestCase):
    """RSS / bosta CPU: mutlak esik; CPU icin taban (gurultu) esigi."""

    def test_rss_within_twenty_percent_is_ok(self):
        self.assertEqual(_absolute("idle_rss_mib", 119.0, 100.0)["status"],
                         "ok")

    def test_rss_over_twenty_percent_fails(self):
        v = _absolute("idle_rss_mib", 121.0, 100.0)
        self.assertEqual(v["status"], "fail")
        self.assertTrue(compare.failed([v]))

    def test_idle_cpu_floor_swallows_noise(self):
        # baseline 0.4 -> %20 esigi 0.48; taban 2.0 kazanir
        self.assertEqual(_absolute("idle_cpu_pct", 1.9, 0.4)["status"], "ok")

    def test_idle_cpu_above_floor_fails(self):
        self.assertEqual(_absolute("idle_cpu_pct", 2.1, 0.4)["status"],
                         "fail")

    def test_zero_cpu_baseline_uses_floor_and_has_no_ratio(self):
        # sifir taban: oran anlamsiz (inf JSON'a sizmamali), taban 2.0
        v = _absolute("idle_cpu_pct", 0.2, 0.0)
        self.assertIsNone(v["ratio"])
        self.assertEqual(v["status"], "ok")
        self.assertEqual(_absolute("idle_cpu_pct", 2.5, 0.0)["status"],
                         "fail")

    def test_idle_cpu_uses_baseline_when_above_floor(self):
        # baseline 65 -> esik 78 (taban 2'yi asar)
        self.assertEqual(
            _absolute("idle_loaded_cpu_pct", 77.0, 65.0)["status"], "ok")
        self.assertEqual(
            _absolute("idle_loaded_cpu_pct", 79.0, 65.0)["status"], "fail")

    def test_absolute_metrics_skip_on_other_environment(self):
        # farkli platform/Python: karsilastirilamaz sayi basarisizlik degil
        v = compare.compare({"idle_rss_mib": 100.0},
                            {"idle_rss_mib": 500.0}, 1.0,
                            absolute_ok=False)[0]
        self.assertEqual(v["status"], "skip")
        self.assertFalse(compare.failed([v]))


class VerdictTest(unittest.TestCase):
    def test_new_metric_does_not_fail(self):
        v = compare.compare({}, {"cdc_mib_s": 10.0}, 1.0)[0]
        self.assertEqual(v["status"], "new")
        self.assertFalse(compare.failed([v]))

    def test_unknown_metric_ignored(self):
        self.assertEqual(compare.compare({}, {"bilinmeyen": 1.0}, 1.0), [])

    def test_table_shows_status(self):
        text = compare.format_table([_rate(100.0), _absolute(
            "idle_rss_mib", 500.0, 100.0)])
        self.assertIn("engine throughput", text)
        self.assertIn("fail", text)
        self.assertIn("ok", text)

    def test_spec_of_covers_all_known_metrics(self):
        for name in list(compare.RATE_METRICS) + list(compare.ABS_METRICS):
            self.assertIsNotNone(compare.spec_of(name))
        self.assertIsNone(compare.spec_of("yok"))


class BaselineFileTest(unittest.TestCase):
    """Depodaki baseline tam ve kendi kendine tutarli olmali."""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(BASELINE.read_text("utf-8"))

    def test_version_and_metadata(self):
        self.assertEqual(self.data.get("version"), compare.BASELINE_VERSION)
        self.assertGreater(float(self.data.get("calibration") or 0), 0.0)
        self.assertTrue(self.data.get("generated"))
        self.assertTrue(self.data.get("python"))
        self.assertTrue(self.data.get("platform"))

    def test_every_known_metric_has_a_baseline(self):
        expected = set(compare.RATE_METRICS) | set(compare.ABS_METRICS)
        self.assertEqual(set(self.data.get("metrics") or {}), expected)

    def test_baseline_values_are_sane(self):
        for name, value in (self.data.get("metrics") or {}).items():
            self.assertIsInstance(value, (int, float), name)
            self.assertGreaterEqual(float(value), 0.0, name)
            if name not in compare.CPU_METRICS:
                self.assertGreater(float(value), 0.0, name)

    def test_baseline_compared_with_itself_is_green(self):
        # ayni degerler + faktor 1: hicbir metrik gerileme sayilmaz
        verdicts = compare.compare(self.data["metrics"],
                                   self.data["metrics"], 1.0, True)
        self.assertFalse(compare.failed(verdicts))
        self.assertTrue(all(v["status"] == "ok" for v in verdicts))


if __name__ == "__main__":
    unittest.main()
