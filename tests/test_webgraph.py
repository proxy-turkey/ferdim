"""Web arayuzu hiz/bağlantı grafikleri: statik denetimler (tarayici gerekmez).

Grafikler istemci tarafinda ring buffer'larla cizilir; sunucuya yeni bir uç
nokta eklenmez (istatistikler zaten hiz ve canli baglanti sayisi veriyor).
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ferdim.i18n import LANGS, S  # noqa: E402

GRAPH_KEYS = {"graph_title", "graph_conn", "graph_speed_a11y",
              "graph_conn_a11y", "spark_a11y"}


class WebGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "web" / "index.html").read_text("utf-8")
        cls.js = (ROOT / "web" / "app.js").read_text("utf-8")
        cls.css = (ROOT / "web" / "style.css").read_text("utf-8")

    def test_01_canvases_are_accessible_images(self):
        for cid in ("speedGraph", "connGraph"):
            m = re.search(r"<canvas\b[^>]*\bid=\"%s\"[^>]*>" % cid, self.html)
            self.assertIsNotNone(m, cid)
            tag = m.group(0)
            self.assertIn('role="img"', tag, cid)
            self.assertIn("aria-label", tag, cid)
            self.assertIn("data-i18n-aria", tag, cid)
        # bolumun erisilebilir adi var
        self.assertIn('id="graphH"', self.html)
        self.assertIn('aria-labelledby="graphH"', self.html)

    def test_02_graph_hidpi_and_no_animation(self):
        self.assertIn("devicePixelRatio", self.js)
        self.assertIn('getContext("2d")', self.js)
        self.assertIn("setTransform", self.js)
        # animasyon dongusu/gecisi yok: azaltilmis hareket tercihine uyulur
        self.assertNotIn("requestAnimationFrame", self.js)
        self.assertNotIn("innerHTML", self.js)
        self.assertIn("prefers-reduced-motion", self.css)

    def test_03_ring_buffers_are_bounded_and_pruned(self):
        for needle in ("HIST_MAX", "histSpeed", "histConn", "jobHist"):
            self.assertIn(needle, self.js)
        self.assertIn("if (arr.length > HIST_MAX) arr.shift();", self.js)
        # is kaybolunca gecmisi de silinir (bellek sizmaz)
        self.assertIn("jobHist.delete(id)", self.js)

    def test_04_graph_reads_existing_stats_fields(self):
        # yeni sunucu alani yok: hiz toplami + is basina canli baglanti sayisi
        self.assertIn("lastStats.total_speed", self.js)
        self.assertIn("j.segments", self.js)
        self.assertIn("graphSpeedNow", self.js)
        self.assertIn("graphConnNow", self.js)

    def test_05_graph_i18n_keys_in_all_languages(self):
        used = set(re.findall(r'data-i18n-aria="([a-z_0-9]+)"', self.html))
        used |= set(re.findall(r'data-i18n="([a-z_0-9]+)"', self.html))
        used |= set(re.findall(r'\btrf?\("([a-z_0-9]+)"\s*[,)]', self.js))
        self.assertTrue(GRAPH_KEYS <= used, GRAPH_KEYS - used)
        for key in GRAPH_KEYS:
            self.assertIn(key, S, key)
            self.assertEqual(set(S[key]), set(LANGS), key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
