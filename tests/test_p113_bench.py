"""1.13 performans kapisi - karsilastirma mantigi (hizli test; olcum YOK).

Gercek benchmark'lar saniyeler surer ve makineye bagimlidir; burada
yalnizca saf karsilastirma/normalizasyon/floor fonksiyonlari ve depodaki
baseline dosyasinin gecerliligi dogrulanir (JSON okuma + aritmetik).
Baseline, 1.13 lane'leri birlestikten sonra integrator tarafindan
yenilenecegi icin metrik adlarinin birebir eslesmesi beklenmez.
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


def _one(name, actual, base=None, cpu=1.0, io=1.0, absolute_ok=True):
    baseline = {} if base is None else {name: base}
    return compare.compare(baseline, {name: actual}, cpu, io,
                           absolute_ok)[0]


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

    def test_absolute_metrics_skip_on_other_environment(self):
        v = _one("idle_loaded_rss_mib", 900.0, base=100.0,
                 absolute_ok=False)
        self.assertEqual(v["status"], "skip")
        self.assertFalse(compare.failed([v]))
        # floor da atlanir: farkli ortamda mutlak sayi karsilastirilamaz
        self.assertFalse(compare.failed([_one(
            "idle_loaded_cpu_pct", 99.0, base=1.0, absolute_ok=False)]))


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

    def test_table_shows_fields(self):
        text = compare.format_table([
            _one("engine_mib_s", 140.0, base=100.0),
            _one("stats_frame_ms", 100.0, base=100.0),
        ])
        for needle in ("engine throughput", "stats frame", "floor", "fail",
                       "ok"):
            self.assertIn(needle, text)


class BaselineFileTest(unittest.TestCase):
    """Depodaki baseline gecerli bir belge olmali (adlar eski olabilir)."""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(BASELINE.read_text("utf-8"))

    def test_version_and_metadata(self):
        self.assertEqual(self.data.get("version"), compare.BASELINE_VERSION)
        self.assertGreater(float(self.data.get("calibration") or 0), 0.0)
        self.assertTrue(self.data.get("generated"))
        self.assertTrue(self.data.get("python"))
        self.assertTrue(self.data.get("platform"))
        self.assertTrue(self.data.get("metrics"))

    def test_baseline_compared_with_itself_is_green(self):
        # ayni degerler + faktor 1: bilinen metrikler icin gerileme yok
        verdicts = compare.compare(self.data["metrics"],
                                   self.data["metrics"], 1.0, 1.0, True)
        self.assertFalse(compare.failed(verdicts))


if __name__ == "__main__":
    unittest.main()
