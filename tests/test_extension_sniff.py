"""Eklenti yanit siniflandiricisi (extension/sniff.js) testleri.

sniff.js saf bir fonksiyondur; tarayici API'si cagirmaz. Burada node ile
dogrudan calistirilir (node yoksa testler atlanir). Kural seti siteye ozel
olamaz: yalnizca URL ve yanit basliklari sinyal sayilir.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNIFF = ROOT / "extension" / "sniff.js"
MANIFEST = ROOT / "extension" / "manifest.json"
NODE = shutil.which("node")

MB = 1048576

HARNESS = r"""'use strict';
const fs = require('fs');
const sniff = require(process.argv[2]);
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = cases.map((c) => {
  try {
    return {ok: true, value: sniff.classify(c.req, c.opts)};
  } catch (e) {
    return {ok: false, error: String((e && e.stack) || e)};
  }
});
process.stdout.write(JSON.stringify(out));
"""


def req(url, ct="", cd="", cl=None, cr="", status=200):
    """webRequest onHeadersReceived benzeri istek tanimi uretir."""
    headers = []
    if ct:
        headers.append({"name": "content-type", "value": ct})
    if cd:
        headers.append({"name": "content-disposition", "value": cd})
    if cl is not None:
        headers.append({"name": "content-length", "value": str(cl)})
    if cr:
        headers.append({"name": "content-range", "value": cr})
    return {"url": url, "status": status, "headers": headers}


@unittest.skipUnless(NODE and SNIFF.is_file(), "node bulunamadi")
class SniffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ferdim-sniff-")
        cls.harness = Path(cls.tmp) / "harness.cjs"
        cls.harness.write_text(HARNESS, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_cases(self, cases):
        res = subprocess.run(
            [NODE, str(self.harness), str(SNIFF)],
            input=json.dumps(cases), text=True, capture_output=True,
            timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(len(out), len(cases))
        for i, r in enumerate(out):
            self.assertTrue(r["ok"], f"case {i}: {r.get('error')}")
        return [r["value"] for r in out]

    def test_hls_content_types(self):
        cases = [
            (req("https://cdn.example/v/master.m3u8",
                 "application/vnd.apple.mpegurl"), "master.m3u8"),
            (req("https://cdn.example/v/playlist", "application/x-mpegurl"),
             "playlist"),
            (req("https://cdn.example/v/audio", "audio/mpegurl; charset=utf-8"),
             "audio"),
        ]
        out = self.run_cases([{"req": c} for c, _ in cases])
        for (r, name), got in zip(cases, out):
            self.assertIsNotNone(got, r["url"])
            self.assertEqual(got["kind"], "hls", r["url"])
            self.assertEqual(got["role"], "manifest", r["url"])
            self.assertFalse(got["segmentish"], r["url"])
            self.assertEqual(got["filename"], name)
        # text/plain bir sayfa turudur; m3u8 uzantisi olmadan medya sayilmaz
        extra = self.run_cases([
            {"req": req("https://cdn.example/v/txt", "text/plain")},
            {"req": req("https://cdn.example/v/list.m3u8", "text/plain")},
        ])
        self.assertIsNone(extra[0])
        self.assertEqual(extra[1]["kind"], "hls")

    def test_dash_content_types(self):
        cases = [
            req("https://cdn.example/d/stream.mpd", "application/dash+xml"),
            req("https://cdn.example/d/stream", "application/dash+xml"),
            req("https://cdn.example/d/stream.mpd",
                "application/octet-stream"),
            # bazi sunucular MPD'yi genel XML turuyle verir; uzanti kazanir
            req("https://cdn.example/d/stream.mpd", "application/xml"),
            req("https://cdn.example/d/other", "application/xml"),
        ]
        out = self.run_cases([{"req": c} for c in cases])
        for c, got in zip(cases[:4], out[:4]):
            self.assertIsNotNone(got, c["url"])
            self.assertEqual(got["kind"], "dash")
            self.assertEqual(got["role"], "manifest")
        self.assertIsNone(out[4])

    def test_direct_media(self):
        cases = [
            (req("https://cdn.example/f/movie.mp4", "video/mp4",
                 'attachment; filename="movie.mp4"', cl=700 * MB), "video",
             "movie.mp4"),
            (req("https://cdn.example/f/track.mp3", "audio/mpeg"), "audio",
             "track.mp3"),
            (req("https://cdn.example/f/clip.webm",
                 "application/octet-stream",
                 'inline; filename="clip.webm"'), "video", "clip.webm"),
        ]
        out = self.run_cases([{"req": c} for c, _, _ in cases])
        for (c, kind, name), got in zip(cases, out):
            self.assertIsNotNone(got, c["url"])
            self.assertEqual(got["kind"], kind)
            self.assertEqual(got["role"], "direct")
            self.assertEqual(got["filename"], name)

    def test_content_disposition_filename(self):
        cases = [
            req("https://cdn.example/dl/1", "application/octet-stream",
                "attachment; filename*=UTF-8''na%C3%AFve%20film.mp4"),
            req("https://cdn.example/dl/2", "application/octet-stream",
                'attachment; filename="../../etc/movie.mkv"'),
            req("https://cdn.example/dl/3", "application/octet-stream",
                "attachment; filename=plain.avi"),
        ]
        out = self.run_cases([{"req": c} for c in cases])
        self.assertEqual(out[0]["filename"], "naïve film.mp4")
        self.assertEqual(out[0]["kind"], "video")
        self.assertEqual(out[1]["filename"], "movie.mkv")  # yol bileseni atilir
        self.assertEqual(out[1]["kind"], "video")
        self.assertEqual(out[2]["filename"], "plain.avi")
        self.assertEqual(out[2]["kind"], "video")

    def test_large_direct_links(self):
        opts = {"minBytes": 1 * MB}
        cases = [
            (req("https://cdn.example/dl/iso", "application/octet-stream",
                 'attachment; filename="distro.iso"', cl=4 * 1024 * MB),
             "archive"),
            (req("https://cdn.example/dl/image",
                 "application/x-iso9660-image"), "archive"),
            (req("https://cdn.example/dl/report", "application/pdf",
                 'attachment; filename="report.pdf"', cl=200 * MB), "file"),
            (req("https://cdn.example/dl/blob", "application/octet-stream",
                 'attachment; filename="data.bin"', cl=8 * MB), "file"),
        ]
        out = self.run_cases([{"req": c, "opts": opts} for c, _ in cases])
        for (c, kind), got in zip(cases, out):
            self.assertIsNotNone(got, c["url"])
            self.assertEqual(got["kind"], kind, c["url"])
            self.assertEqual(got["role"], "direct")
        self.assertEqual(out[0]["size"], 4 * 1024 * MB)

    def test_size_threshold(self):
        opts = {"minBytes": 1 * MB}
        cases = [
            req("https://cdn.example/f/small.mp4", "video/mp4", cl=500_000),
            req("https://cdn.example/f/tiny.txt", "application/octet-stream",
                'attachment; filename="tiny.txt"', cl=4096),
            req("https://cdn.example/f/track.mp3", "audio/mpeg", cl=500_000),
            req("https://cdn.example/f/master.m3u8",
                "application/vnd.apple.mpegurl", cl=900),
            req("https://cdn.example/f/unknown.bin",
                "application/octet-stream",
                'attachment; filename="unknown.bin"'),
        ]
        out = self.run_cases([{"req": c, "opts": opts} for c in cases])
        self.assertIsNone(out[0])                       # kucuk video
        self.assertIsNone(out[1])                       # kucuk dosya
        self.assertEqual(out[2]["kind"], "audio")       # ses esige takilmaz
        self.assertEqual(out[3]["role"], "manifest")    # manifest her boyutta
        self.assertEqual(out[4]["kind"], "file")        # boyut bilinmiyor
        self.assertEqual(out[4]["size"], 0)

    def test_fragment_requests_are_noise(self):
        cases = [
            req("https://cdn.example/hls/seg-003.ts", "video/mp2t",
                cl=1400, cr="bytes 0-1399/48211", status=206),
            req("https://cdn.example/hls/seg-004.m4s", "video/iso.segment",
                cl=900, cr="bytes 0-899/9123", status=206),
            req("https://cdn.example/hls/audio-1.aac", "audio/aac",
                cl=800, cr="bytes 0-799/90000", status=206),
            req("https://cdn.example/hls/full.ts", "video/mp2t", cl=50 * MB),
            req("https://cdn.example/f/movie.mp4", "video/mp4",
                cl=1024, cr="bytes 0-1023/123456789", status=206),
        ]
        out = self.run_cases([{"req": c} for c in cases])
        for got in out[:3]:
            self.assertEqual(got["role"], "segment")
            self.assertTrue(got["segmentish"])
        # tam yanit: dogrudan .ts baglantisi hala yakalanir
        self.assertEqual(out[3]["role"], "direct")
        self.assertTrue(out[3]["segmentish"])
        # mp4 aralik yaniti parca degil; boyut Content-Range toplamindan
        self.assertEqual(out[4]["role"], "direct")
        self.assertFalse(out[4]["segmentish"])
        self.assertEqual(out[4]["size"], 123456789)

    def test_page_assets_ignored(self):
        cases = [
            req("https://site.example/index.html", "text/html"),
            req("https://site.example/media/movie.mp4", "text/html"),
            req("https://site.example/share", "image/jpeg",
                'attachment; filename="photo.jpg"'),
            req("https://site.example/api/list", "application/json"),
            req("https://site.example/style.css", "text/css"),
            req("https://site.example/download/12345",
                "application/octet-stream"),
            req("https://site.example/"),                       # dosya adi yok
            req("chrome-extension://abc/x.mp4", "video/mp4"),
        ]
        out = self.run_cases([{"req": c} for c in cases])
        for c, got in zip(cases, out):
            self.assertIsNone(got, c["url"])

    def test_malformed_input_never_throws(self):
        cases = [
            {},
            {"url": None},
            {"url": "https://cdn.example/f/a.mp4",
             "headers": [None, {"name": "Content-Type"}]},
            {"url": "https://cdn.example/f/a.mp4", "status": "206",
             "headers": [{"name": "content-range", "value": "bytes ?"}]},
            {"url": "https://cdn.example/f/a.mp4",
             "headers": [{"name": "content-type", "value": None}]},
        ]
        out = self.run_cases([{"req": c} for c in cases])
        self.assertIsNone(out[0])
        self.assertIsNone(out[1])
        self.assertEqual(out[2]["kind"], "video")   # deger yok: uzantidan
        self.assertEqual(out[4]["kind"], "video")

    def test_classification_is_option_independent_for_kind(self):
        """Ses filtresi background'da; siniflandirici opts'a gore tur degistirmez."""
        r = req("https://cdn.example/f/track.mp3", "audio/mpeg")
        out = self.run_cases([{"req": r, "opts": {"minBytes": 100 * MB}},
                              {"req": r, "opts": {}},
                              {"req": r, "opts": None}])
        for got in out:
            self.assertEqual(got["kind"], "audio")
            self.assertEqual(got["role"], "direct")


class ManifestWiringTests(unittest.TestCase):
    def test_sniff_loaded_before_background(self):
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        scripts = m["background"]["scripts"]
        self.assertIn("sniff.js", scripts)
        self.assertLess(scripts.index("sniff.js"), scripts.index("background.js"))
        for name in scripts:
            self.assertTrue((ROOT / "extension" / name).is_file(), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
