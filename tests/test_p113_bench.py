"""1.13 performans kapisi - karsilastirma mantigi (hizli test; olcum YOK).

Gercek benchmark'lar saniyeler surer ve makineye bagimlidir; burada
yalnizca saf karsilastirma/normalizasyon/floor/ortam fonksiyonlari ve
depodaki baseline dosyasinin gecerliligi dogrulanir (JSON okuma +
aritmetik).
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


def _one(name, actual, base=None, cpu=1.0, io=1.0, env=True):
    baseline = {} if base is None else {name: base}
    return compare.compare(baseline, {name: actual}, cpu, io, env)[0]


class RateMetricTest(unittest.TestCase):
    """Hiz: yuksek iyi, secilen kalibrasyonla olcekli, drift + floor."""

    def test_cpu_rate_scales_with_cpu_factor(self):
        # parcalayici CPU'ya bagli: 2x hizli makinede 2x beklenir
        v = _one("cdc_mib_s", 200.0, base=100.0, cpu=2.0)
        self.assertAlmostEqual(v["target"], 200.0)
        self.assertEqual(v["status"], "ok")
        v = _one("cdc_mib_s", 140.0, base=100.0, cpu=2.0)   # %30 gerileme
        self.assertEqual(v["status"], "fail")
        self.assertEqual(v["note"], "drift")

    def test_engine_uses_io_not_cpu_factor(self):
        # motor loopback yolu: CPU 4x hizli olsa da hedef degismez
        v = _one("engine_mib_s", 200.0, base=200.0, cpu=4.0, io=1.0)
        self.assertAlmostEqual(v["target"], 200.0)
        self.assertEqual(v["status"], "ok")
        v = _one("engine_mib_s", 150.0, base=200.0, cpu=4.0, io=1.0)
        self.assertEqual(v["status"], "fail")

    def test_io_rate_scales_with_io_factor(self):
        v = _one("queue_bulk_add_jobs_s", 160.0, base=100.0, io=2.0)
        self.assertAlmostEqual(v["target"], 200.0)
        self.assertEqual(v["status"], "ok")
        self.assertEqual(_one("queue_bulk_add_jobs_s", 140.0, base=100.0,
                              io=2.0)["status"], "fail")

    def test_exactly_twenty_percent_drop_is_still_ok(self):
        # esik "fazla" gerileme: tam %20 gecer
        self.assertEqual(_one("cdc_mib_s", 80.0, base=100.0)["status"], "ok")
        self.assertEqual(_one("cdc_mib_s", 79.0, base=100.0)["status"],
                         "fail")

    def test_engine_floor_is_absolute(self):
        # yavas baseline'a gore drift yok ama urun tabani (150) altinda
        v = _one("engine_mib_s", 140.0, base=100.0)
        self.assertEqual(v["status"], "fail")
        self.assertIn("floor", v["note"])
        self.assertEqual(_one("engine_mib_s", 160.0, base=100.0)["status"],
                         "ok")


class EnvironmentTest(unittest.TestCase):
    """Ortam kapisi: cekirdege duyarli metriklerde drift atlanir."""

    SAME = {"platform": "linux", "python": "3.14.10", "cpu_count": 12}

    def test_environments_match(self):
        self.assertTrue(compare.environments_match(self.SAME, dict(self.SAME)))
        # eski baseline'da cpu_count yok: bilinmeyen alan uyusmazlik degil
        old = dict(self.SAME)
        old.pop("cpu_count")
        self.assertTrue(compare.environments_match(old, dict(self.SAME)))

    def test_cpu_count_mismatch(self):
        current = dict(self.SAME, cpu_count=2)
        self.assertFalse(compare.environments_match(self.SAME, current))

    def test_python_minor_mismatch(self):
        current = dict(self.SAME, python="3.13.7")
        self.assertFalse(compare.environments_match(self.SAME, current))

    def test_platform_mismatch(self):
        current = dict(self.SAME, platform="darwin")
        self.assertFalse(compare.environments_match(self.SAME, current))

    def test_engine_drift_skipped_on_env_mismatch(self):
        # CI kosucusu: 192 MiB/s, dev baseline 405, io factor 1.19
        v = _one("engine_mib_s", 192.1, base=405.3, io=1.19, env=False)
        self.assertEqual(v["status"], "skip")
        self.assertEqual(v["note"], "different environment")
        self.assertIsNone(v["limit"])
        self.assertFalse(compare.failed([v]))

    def test_engine_floor_still_applies_on_env_mismatch(self):
        v = _one("engine_mib_s", 120.0, base=405.3, io=1.19, env=False)
        self.assertEqual(v["status"], "fail")
        self.assertIn("floor", v["note"])

    def test_cpu_rate_drift_survives_env_mismatch(self):
        # kalibrasyonla olceklenen metrik ortam farkindan bagimsizdir
        v = _one("cdc_mib_s", 60.0, base=100.0, env=False)
        self.assertEqual(v["status"], "fail")
        self.assertEqual(v["note"], "drift")


class LatencyMetricTest(unittest.TestCase):
    """Gecikme: dusuk iyi, CPU faktoruyle ters olcekli, floor cpu'ya bagli."""

    def test_latency_scales_inversely(self):
        # 2x hizli makinede hedef yariya iner
        v = _one("stats_frame_ms", 55.0, base=100.0, cpu=2.0)
        self.assertAlmostEqual(v["target"], 50.0)
        self.assertAlmostEqual(v["limit"], 60.0)
        self.assertEqual(v["status"], "ok")
        self.assertEqual(_one("stats_frame_ms", 65.0, base=100.0,
                              cpu=2.0)["status"], "fail")

    def test_latency_floor_is_cpu_normalised(self):
        # floor 150 ms / cpu 2 -> bu makinede 75 ms
        v = _one("stats_frame_ms", 80.0, base=140.0, cpu=2.0)
        self.assertEqual(v["status"], "fail")
        self.assertIn("floor", v["note"])
        self.assertEqual(_one("stats_frame_ms", 70.0, base=140.0,
                              cpu=2.0)["status"], "ok")

    def test_frame_floor_applies_without_baseline(self):
        v = _one("stats_frame_ms", 160.0)
        self.assertEqual(v["status"], "fail")
        self.assertIn("floor", v["note"])
        self.assertEqual(_one("stats_frame_ms", 100.0)["status"], "new")


class AbsoluteMetricTest(unittest.TestCase):
    """RSS / bosta CPU: mutlak esik, gurultu tabani ve urun tabani."""

    def test_rss_drift(self):
        self.assertEqual(_one("idle_loaded_rss_mib", 119.0, base=100.0)
                         ["status"], "ok")
        self.assertEqual(_one("idle_loaded_rss_mib", 121.0, base=100.0)
                         ["status"], "fail")

    def test_rss_floor(self):
        # drift serbest (limit 336) ama 300 MiB urun tabani asildi
        v = _one("idle_loaded_rss_mib", 310.0, base=280.0)
        self.assertEqual(v["status"], "fail")
        self.assertEqual(v["note"], "floor")

    def test_idle_cpu_noise_floor(self):
        # baseline 0.4: %20 esigi 0.48; 2.0 tabani kazanir
        self.assertEqual(_one("idle_cpu_pct", 1.9, base=0.4)["status"], "ok")
        self.assertEqual(_one("idle_cpu_pct", 2.1, base=0.4)["status"],
                         "fail")

    def test_idle_cpu_product_floor(self):
        # baseline 4.5 -> drift siniri 5.4; 5.0 tabani ayrica uygulanir
        self.assertEqual(_one("idle_loaded_cpu_pct", 5.05, base=4.5)
                         ["status"], "fail")
        self.assertEqual(_one("idle_loaded_cpu_pct", 4.9, base=4.5)
                         ["status"], "ok")

    def test_sse_cpu_has_no_product_floor(self):
        # SSE varyanti yalniz drift ile denetlenir (floor yok)
        v = _one("idle_sse_cpu_pct", 7.0, base=6.0)
        self.assertEqual(v["status"], "ok")
        self.assertIsNone(v["floor"])

    def test_absolute_drift_skipped_on_env_mismatch(self):
        # farkli ortam: drift yok, floor var (300 MiB tabaninin altinda)
        v = _one("idle_loaded_rss_mib", 200.0, base=100.0, env=False)
        self.assertEqual(v["status"], "skip")
        self.assertIsNone(v["limit"])
        self.assertFalse(compare.failed([v]))
        v = _one("idle_loaded_cpu_pct", 3.0, base=0.4, env=False)
        self.assertEqual(v["status"], "skip")

    def test_absolute_floor_still_applies_on_env_mismatch(self):
        v = _one("idle_loaded_cpu_pct", 6.0, base=0.4, env=False)
        self.assertEqual(v["status"], "fail")
        self.assertEqual(v["note"], "floor")
        v = _one("idle_loaded_rss_mib", 350.0, base=100.0, env=False)
        self.assertEqual(v["status"], "fail")
        self.assertEqual(v["note"], "floor")


class FloorsOnlyTest(unittest.TestCase):
    """Baseline yokken (CI runner baseline'i commit edilmeden) yalniz floor."""

    def test_missing_baseline_marks_new_but_checks_floors(self):
        metrics = {"engine_mib_s": 200.0, "stats_frame_ms": 100.0,
                   "cdc_mib_s": 1.0}
        verdicts = compare.compare({}, metrics)
        self.assertEqual({v["status"] for v in verdicts}, {"new"})
        self.assertFalse(compare.failed(verdicts))

    def test_missing_baseline_engine_floor_fails(self):
        v = compare.compare({}, {"engine_mib_s": 120.0})[0]
        self.assertEqual(v["status"], "fail")
        self.assertIn("floor", v["note"])

    def test_missing_baseline_frame_floor_fails(self):
        v = compare.compare({}, {"stats_frame_ms": 200.0})[0]
        self.assertEqual(v["status"], "fail")
        self.assertIn("floor", v["note"])

    def test_missing_baseline_absolute_floor_fails(self):
        v = compare.compare({}, {"idle_queued_cpu_pct": 6.0})[0]
        self.assertEqual(v["status"], "fail")
        self.assertIn("floor", v["note"])


class VerdictTest(unittest.TestCase):
    def test_new_rate_metric_does_not_fail(self):
        v = _one("queue_load_jobs_s", 10.0)
        self.assertEqual(v["status"], "new")
        self.assertFalse(compare.failed([v]))

    def test_unknown_metric_ignored(self):
        self.assertEqual(compare.compare({}, {"bilinmeyen": 1.0}, 1.0, 1.0),
                         [])

    def test_floor_metrics_are_declared(self):
        expected = {"engine_mib_s", "stats_frame_ms",
                    "idle_loaded_cpu_pct", "idle_queued_cpu_pct",
                    "idle_loaded_rss_mib", "idle_queued_rss_mib",
                    "idle_sse_rss_mib"}
        self.assertEqual(set(compare.FLOOR_METRICS), expected)
        self.assertNotIn("idle_sse_cpu_pct", compare.FLOOR_METRICS)

    def test_env_bound_metrics_are_engine_and_absolute(self):
        absolute = {name for name, m in compare.METRICS.items()
                    if m.family == "absolute"}
        self.assertEqual(set(compare.ENV_BOUND_METRICS),
                         absolute | {"engine_mib_s"})
        self.assertNotIn("queue_load_jobs_s", compare.ENV_BOUND_METRICS)

    def test_table_shows_fields(self):
        text = compare.format_table([
            _one("engine_mib_s", 140.0, base=100.0),
            _one("stats_frame_ms", 100.0, base=100.0),
        ])
        for needle in ("engine throughput", "stats frame", "floor", "fail",
                       "ok"):
            self.assertIn(needle, text)


class BaselineFileTest(unittest.TestCase):
    """Depodaki gelistirme baseline'i gecerli bir belge olmali."""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(BASELINE.read_text("utf-8"))

    def test_version_and_metadata(self):
        self.assertEqual(self.data.get("version"), compare.BASELINE_VERSION)
        self.assertGreater(float(self.data.get("calibration") or 0), 0.0)
        self.assertTrue(self.data.get("generated"))
        self.assertTrue(self.data.get("python"))
        self.assertTrue(self.data.get("platform"))
        # ortam alani: cekirdek sayisi baseline'da kayitli olmali
        self.assertIsInstance(self.data.get("cpu_count"), int)
        self.assertGreater(self.data["cpu_count"], 0)
        self.assertTrue(self.data.get("metrics"))

    def test_baseline_compared_with_itself_is_green(self):
        # ayni degerler + faktor 1 + ayni ortam: gerileme yok
        verdicts = compare.compare(self.data["metrics"],
                                   self.data["metrics"], 1.0, 1.0, True)
        self.assertFalse(compare.failed(verdicts))


if __name__ == "__main__":
    unittest.main()
