"""ROADMAP 1.8: fuzz duman testi (deterministik, hizli).

Her hedef sabit `seed` ve hedefe ozel girdi sayisi ile kosar; boylece
ayni girdi dizisi her makinede ayni bulgu siniflarini uretir (sure
siniri yerine sayac kullanilir). Beklenen istisnalar disindaki siniflar
`tests/fuzz/known.json` ile karsilastirilir: bilinen siniflar yesil
kalir, YENI bir sinif (or. yeni bir IndexError) testi kirmizi yapar.

Ayrica XML varlik sismesi (billion laughs) davranisi burada acikca
denetlenir: ayristirici ya reddetmeli ya da cikti sinirli kalmali.
"""
from __future__ import annotations

import json
import sys
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import metalink
from ferdim.engine import dash
from tests.fuzz import (SMOKE_ITERATIONS, SMOKE_SEED, TARGETS, load_known,
                        run_target, seeds_clean, xml_bomb_doc, xml_bomb_mpd)
from tests.fuzz.targets import MPD_URL, _text
from tests.helpers import tmp_root

# XML bomb genisletilirse ~9 MB'lik tek bir URL olusur; kabul edilirse
# test bunu yakalar (bellek patlamasi degil, sinirli bir kabul).
BOMB_MAX_OUTPUT = 1 << 20
BOMB_SECONDS = 2.0


class FuzzSmokeTest(unittest.TestCase):
    def test_known_entries_reference_real_targets(self):
        known = load_known()
        for ident in known:
            target, _, kind = ident.partition(":")
            self.assertIn(target, TARGETS, f"bilinmeyen hedef: {ident}")
            self.assertTrue(kind, f"sinif kimligi bos: {ident}")

    def test_seed_corpus_is_expected(self):
        """Tohumlar beklenmeyen istisna ya da asilma uretmemeli."""
        root = tmp_root(self)
        for name, target in TARGETS.items():
            with self.subTest(target=name):
                seeds_clean(target, root / name)

    def test_no_new_crash_classes(self):
        known = load_known()
        root = tmp_root(self)
        new_all = []
        for name, target in TARGETS.items():
            with self.subTest(target=name):
                result = run_target(
                    target, seed=SMOKE_SEED,
                    iterations=SMOKE_ITERATIONS[name], timeout=2.0,
                    memory_limit=64 << 20, workdir=root / name)
                self.assertGreater(result.inputs, 0)
                new_all.extend(result.new(known))
        if new_all:
            report = "\n\n".join(f.report() for f in new_all)
            self.fail("yeni fuzz bulgu sinifi (parser sahibi duzeltmeli; "
                      "duzeltme gelene kadar tests/fuzz/known.json'a "
                      "eklenebilir):\n\n" + report)

    def test_xml_entity_expansion_bounded(self):
        """Billion laughs: ya ValueError/ParseError ya sinirli cikti."""
        cases = (
            ("metalink", lambda: metalink.parse(_text(xml_bomb_doc()))),
            ("dash", lambda: dash.build_segment_list(
                _text(xml_bomb_mpd()), MPD_URL)),
        )
        for label, call in cases:
            with self.subTest(parser=label):
                started = time.monotonic()
                try:
                    out = call()
                except (ValueError, ET.ParseError, dash.DashError):
                    self.assertLess(time.monotonic() - started, BOMB_SECONDS)
                    continue
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, BOMB_SECONDS,
                                f"{label}: varlik sismesi yavas")
                size = len(json.dumps(out, default=str))
                self.assertLess(size, BOMB_MAX_OUTPUT,
                                f"{label}: varlik sismesi kabul edildi "
                                f"({size} bayt cikti)")


if __name__ == "__main__":
    unittest.main()
