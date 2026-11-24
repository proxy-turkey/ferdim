"""Yerellestirilmis man sayfalari (docs/tr/*.1) icin tutarlilik testleri.

Uc kural:
  * hem Ingilizce hem Turkce sayfa `man --warnings -l` ile uyarisiz
    bicimlenmeli (man yoksa test atlanir),
  * ayni sayfanin Turkce .TH surum dizesi Ingilizceninkiyle ayni olmali;
    surum yukseltmesinde docs/tr/*.1 dosyalarinin .TH satiri da
    guncellenmelidir,
  * Ingilizce sayfadaki her uzun secenek (\\-\\-foo) Turkce sayfada da
    bulunmali; secenek adlari cevrilmez.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
TR = DOCS / "tr"
PAGES = ("ferdim.1", "ferdim-nm.1", "ferdim-gui.1")

MAN = shutil.which("man")

# roff'ta tire "\\-" olarak kacirilir ve secenek adi "\fB" gibi font
# kacislariyla sarilir; ikisini de normalize ettikten sonra uzun
# secenekleri topluyoruz (\fB\-\-older\-than\fR -> older-than)
_FONT_RE = re.compile(r"\\f[BRIP0-9]")
_OPT_RE = re.compile(r"(?<![\w-])--([a-z0-9][a-z0-9-]*)")


def _man_env():
    env = dict(os.environ)
    env["LC_ALL"] = "C.UTF-8"
    env["MANWIDTH"] = "80"
    env.pop("MAN_KEEP_FORMATTING", None)
    return env


def _pages():
    for name in PAGES:
        yield DOCS / name, TR / name


def _th_version(path):
    """.TH satirindaki surum alanini dondurur (tirnakli ikinci alan)."""
    for line in path.read_text("utf-8").splitlines():
        if line.startswith(".TH"):
            fields = re.findall(r'"([^"]*)"', line)
            if len(fields) < 2:
                raise AssertionError(f"{path}: .TH surum alani yok: {line}")
            return fields[1]
    raise AssertionError(f"{path}: .TH satiri yok")


def _long_options(path):
    text = _FONT_RE.sub("", path.read_text("utf-8").replace("\\-", "-"))
    return set(_OPT_RE.findall(text))


class ManPageTest(unittest.TestCase):
    def test_00_tr_pages_exist(self):
        for en, tr in _pages():
            self.assertTrue(en.is_file(), en)
            self.assertTrue(tr.is_file(), tr)
            self.assertTrue(
                tr.read_text("utf-8").startswith('.\\" -*- coding: UTF-8 -*-'),
                f"{tr}: UTF-8 kodlama bildirimi ilk satirda olmali")

    @unittest.skipUnless(MAN, "man komutu yok")
    def test_01_pages_render_without_warnings(self):
        for en, tr in _pages():
            for path in (en, tr):
                with self.subTest(page=str(path.relative_to(ROOT))):
                    proc = subprocess.run(
                        [MAN, "--warnings", "-l", str(path)],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=_man_env(),
                        text=True,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stderr.strip(), "",
                                     f"groff uyarisi: {path.name}")
                    self.assertTrue(proc.stdout.strip(), "bos cikti")

    def test_02_th_version_matches_english(self):
        for en, tr in _pages():
            with self.subTest(page=tr.name):
                self.assertEqual(_th_version(tr), _th_version(en),
                                 f"{tr.name}: .TH surumu Ingilizce sayfayla "
                                 f"ayni olmali")

    def test_03_long_options_present(self):
        for en, tr in _pages():
            missing = _long_options(en) - _long_options(tr)
            with self.subTest(page=tr.name):
                self.assertFalse(missing,
                                 f"{tr.name}: eksik uzun secenekler: "
                                 f"{sorted(missing)}")


if __name__ == "__main__":
    unittest.main()
