"""known.json'dan kapatilan fuzz siniflari icin kalici regresyon testleri.

Her test, fuzz bulgusunun kucultulmus `sample_hex` ornegini (ya da ayni
bozulmayi ureten en kucuk belgeyi) dogrudan kullanir. Sinif geri gelirse
`tests/fuzz` duman testi kirilir (kayit known.json'dan silindi); bu dosya
hatanin nedenini ve beklenen davranisi netlestirir.

Kapatilan siniflar: dash:re.error, dash:Timeout, archive:zlib.error,
archive:NotImplementedError, archive:compression.zstd.ZstdError.
`merkle:RecursionError` bilerek known.json'da kalir (cdc.py sahibi duzeltir).
"""
from __future__ import annotations

import io
import sys
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import archive
from ferdim.engine import dash
from tests.helpers import tmp_root

MPD_URL = "https://cdn.example/vod/manifest.mpd"

# ---------------------------------------------------- kapatilan fuzz ornekleri
# dash:re.error -- SegmentTimeline S@t="\1" re.sub yer tutucusu sayiliyordu.
DASH_RE_ERROR = (
    "3c3f6c203f3e0a3c4d504420653d22223e3c506572696f642064753d225053223e0a3c4164"
    "6170746174696f6e536574206d693d22767034223e3c526570726573656e746174696f6e20"
    "69683d22223e0a20203c5365676d656e7454656d706c617465206d653d227334732220696e"
    "3d226934223e20203c5365676d656e7454696d656c696e653e0a2020203c5320743d225c31"
    "2220643d2230222f3e0a203c2f5365676d656e7454696d656c696e653e3c2f5365676d65"
    "6e7454656d706c6174653e0a3c2f526570726573656e746174696f6e3e3c2f4164617074"
    "6174696f6e5365743e3c2f506572696f643e3c2f4d50443e0a"
)

# dash:Timeout -- SegmentTimeline S@r=999999999999999999999 (milyarlarca URL).
DASH_HUGE_REPEAT = (
    "3c3f786d6c2076657273696f6e3d22312e30223f3e0a3c4d504420747970653d2273746174"
    "6963223e3c506572696f64206475726174696f6e3d225054313253223e0a3c416461707461"
    "74696f6e536574206d696d65547970653d22766964656f2f6d7034223e3c52657072657365"
    "6e746174696f6e2069643d2276222077696474683d22363430220a20206865696768743d22"
    "333630222062616e6477696474683d22393030303030223e0a20203c5365676d656e7454"
    "656d706c617465206d656469613d22732d244e756d626572242d2454696d65242e6d347322"
    "20696e697469616c697a6174696f6e3d22692e6d7034223e0a202020203c5365676d656e"
    "7454696d656c696e653e0a2020202020203c5320743d22302220643d22343030302220723d"
    "2232222f3e0a2020202020203c5320643d22343030302220723d22393939393939393939"
    "3939393939393939393939393939222f3e0a202020203c2f5365676d656e7454696d656c"
    "696e653e0a20203c2f5365676d656e7454656d706c6174653e0a3c2f526570726573656e"
    "746174696f6e3e3c2f41646170746174696f6e5365743e3c2f506572696f643e3c2f4d50"
    "443e0a"
)

# archive:zlib.error -- ic ZIP uyesinin deflate akisi bozuk.
ARCHIVE_INNER_DEFLATE = (
    "504b0304140000000800ac43785dbe8aa3a787000000e900000009000000696e6e65722e7a"
    "69700bf0666611616060e06058e35c11bbf1bd788704030343041303032b030343a25e494"
    "5c9699f300f1dad733ac11a6b7d74cf3d3925f3b4af68e54156860014f4cf146698b133303"
    "0e83032307082b4e694e82781b47b7b49b11e616208f066641261c66d150c343032c02c46"
    "d7826e05428b35b28501deac6c2061260626862c0606864cb02200504b0102140314000000"
    "0800ac43785d37c76b1e0d000000a000000005000000000000000000000080010000000061"
    "2e747874504b01021403140000000800ac43785d264c0bb718010000000400000b00000000"
    "0000000000000080013000000064697a696e2f622e62696e504b01021403140000000800ac"
    "43785dbe8aa3a787000000e9000000090000000000000000000000800171010000696e6e"
    "65722e7a6970504b05060000000003000300a30000001f0200000000"
)

# archive:NotImplementedError -- ic uye guclu sifreleme bayragi tasiyor.
ARCHIVE_INNER_ENCRYPTED = (
    "01504b0304140000000800ac43785dbe8aa3a787000000e900000009000000696e6e65722e7a"
    "69700bf0666611616060e06058e35c11bbf1bd788704030343041303032b030343a25e494"
    "5c9699f300f1dad733ac11a6b7d74cf3d3925f3b4af68e54156860014adcf146698b133303"
    "0e83032307082b4e694e82781b47b7b49b11e616208f066641261c66d150c343032c02c46"
    "d7826e05428b35b28501deac6c2061260626862c0606864cb02200504b0102140314000000"
    "0800ac43785d37c76b1e0d000000a000000005000000000000000000000080010000000061"
    "2e747874504b01021403140000000800ac43785d264c0bb718010000000400000b00000000"
    "0000000000000080013000000064697a696e2f622e62696e504b01021403140040000800ac"
    "43785dbe8aa3a787000000e9000000090000000000000000000000800171010000696e6e"
    "65722e7a6970504b05060000000003000300a30000001f0200000000"
)

# archive:compression.zstd.ZstdError -- 3.14'te yontem 93 (zstd) bozuk akis.
ARCHIVE_INNER_ZSTD = (
    "504b0304140000000800ac43785dbe8aa3a787000000e900000009000000696e6e65722e7a"
    "69700bf0666611616060e06058e35c11bbf1bd788704030343041303032b030343a25e494"
    "5c9699f300f1dad733ac11a6b7d74cf3d3925f3b4af68e54156860014adcf146698b133303"
    "0e83032307082b4e694e82781b47b7b49b11e616208f066641261c66d150c343032c02c46"
    "d7826e05428b35b28501deac6c2061260626862c0606864cb02200504b0102140314000000"
    "0800ac43785d37c76b1e0d000000a000000005000000000000000000000080010000000061"
    "2e747874504b01021403140000000800ac43785d264c0bb718010000000400000b00000000"
    "0000000000000080013000000064697a696e2f622e62696e504b01021403140000005d00ac"
    "43785dbe8aa3a787000000e9000000090000000000000000000000800171010000696e6e"
    "65722e7a6970504b05060000000003000300a30000001f0200000000"
)


# archive:NotImplementedError (ust arsiv) -- merkezi dizin "zip file version 14.8".
ARCHIVE_ZIP_VERSION = (
    "504b01021403140000000000a745785da07fe8b29001000090010000050000000000000000"
    "000000800100000000732e747874504b01021403140000000000a745785dd22c0ca7e90000"
    "00e90000000900000000000000000000008001b3010000696e6e65722e7a6970504b010214"
    "0394000000000000002100baf7ebc10500000005000000030000000000000000000000ffa1"
    "c3020000626167504b050600000000030003009b000000e90200000000"
)


def _text(hex_sample: str) -> str:
    return bytes.fromhex(hex_sample).decode("utf-8", "replace")


def _mpd(segment_template: str, period: str = 'duration="PT12S"',
         period_extra: str = "") -> str:
    return ('<MPD type="static"><Period %s>'
            '<AdaptationSet mimeType="video/mp4">'
            '<Representation id="v" width="1" height="1" bandwidth="1">'
            '%s</Representation></AdaptationSet></Period></MPD>'
            % (period, segment_template))


# ---------------------------------------------------------------- DASH
class DashRegressionTest(unittest.TestCase):
    def test_time_placeholder_is_literal(self):
        """S@t bir yer tutucu sablonu degil duz metindir ('\\1' patlamamali)."""
        mpd = _mpd('<SegmentTemplate media="s-$Time$-$Number$.m4s" '
                   'initialization="i.mp4"><SegmentTimeline>'
                   '<S t="\\1" d="1"/><S t="0" d="1" r="1"/>'
                   '</SegmentTimeline></SegmentTemplate>')
        init, urls = dash.build_segment_list(mpd, MPD_URL)
        self.assertEqual(init, "https://cdn.example/vod/i.mp4")
        self.assertEqual(urls, ["https://cdn.example/vod/s-\\1-1.m4s",
                                "https://cdn.example/vod/s-0-2.m4s",
                                "https://cdn.example/vod/s-0-3.m4s"])
        # fuzz ornegi: media bos + t='\1' -> re.error degil, tek URL
        _, urls = dash.build_segment_list(_text(DASH_RE_ERROR), MPD_URL)
        self.assertEqual(len(urls), 1)

    def test_timeline_repeat_is_capped(self):
        """S@r sinirsiz tekrar edemez: biriktirirken sinir asilirsa DashError."""
        two = ('<SegmentTemplate media="s$Number$" initialization="i.mp4">'
               '<SegmentTimeline><S t="0" d="1" r="1"/>'
               '<S d="1" r="1"/></SegmentTimeline></SegmentTemplate>')
        with mock.patch.object(dash, "MAX_SEGMENTS", 4):
            self.assertEqual(len(dash.build_segment_list(_mpd(two),
                                                        MPD_URL)[1]), 4)
        with mock.patch.object(dash, "MAX_SEGMENTS", 3):
            with self.assertRaises(dash.DashError) as ctx:
                dash.build_segment_list(_mpd(two), MPD_URL)
            self.assertIn("ust sinirin", str(ctx.exception))
        # fuzz ornegi: r=999999999999999999999 -> URL uretmeden DashError
        with self.assertRaises(dash.DashError):
            dash.build_segment_list(_text(DASH_HUGE_REPEAT), MPD_URL)

    def test_duration_count_is_capped(self):
        """duration/timescale/Period suresi milyarlarca segment uretemez."""
        huge = ('<SegmentTemplate media="s$Number$" duration="1" '
                'timescale="1000000"/>')
        with self.assertRaises(dash.DashError) as ctx:
            dash.build_segment_list(
                _mpd(huge, period='duration="PT3600S"'), MPD_URL)
        self.assertIn("ust sinirin", str(ctx.exception))
        three = '<SegmentTemplate media="s$Number$" duration="1"/>'
        with mock.patch.object(dash, "MAX_SEGMENTS", 4):
            self.assertEqual(len(dash.build_segment_list(
                _mpd(three, period='duration="PT4S"'), MPD_URL)[1]), 4)
            with self.assertRaises(dash.DashError):
                dash.build_segment_list(
                    _mpd(three, period='duration="PT5S"'), MPD_URL)

    def test_period_lookup_avoids_element_truthiness(self):
        """Cocuksuz eleman 'or' ile falsy sayilmamali (AGENTS.md gotcha)."""
        mpd = _mpd('<SegmentTemplate media="s$Number$" duration="1"/>',
                   period='duration="PT3S"')
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            _, urls = dash.build_segment_list(mpd, MPD_URL)
        self.assertEqual(len(urls), 3)


# --------------------------------------------------------------- arsivler
class ArchiveRegressionTest(unittest.TestCase):
    def setUp(self):
        self.root = tmp_root(self, "ferdim-fuzz-")

    def _write(self, hex_sample: str, name: str) -> Path:
        path = self.root / name
        path.write_bytes(bytes.fromhex(hex_sample))
        return path

    def test_corrupt_inner_archive_is_a_note(self):
        """Bozuk/sifreli/desteklenmeyen ic arsiv describe()'u dusurmemeli."""
        cases = (("deflate", ARCHIVE_INNER_DEFLATE),
                 ("sifreli", ARCHIVE_INNER_ENCRYPTED),
                 ("zstd", ARCHIVE_INNER_ZSTD))
        for label, hex_sample in cases:
            with self.subTest(case=label):
                path = self._write(hex_sample, f"case-{label}.zip")
                info = archive.describe(path)          # istisna yok
                entries = {e["path"]: e for e in info["entries"]}
                inner = entries[f"{path.name}!/inner.zip"]
                self.assertIn("ic arsiv okunamadi", inner.get("note", ""))
                # kayit listelenmeye devam eder, alt uyeler acilmaz
                self.assertEqual(inner["kind"], "file")
                self.assertEqual(len(entries), 3)

    def test_unsupported_zip_version_is_archive_error(self):
        """Ust arsiv 'zip file version 14.8' -> ArchiveError, sizinti yok."""
        path = self._write(ARCHIVE_ZIP_VERSION, "version.zip")
        with self.assertRaises(archive.ArchiveError):
            archive.describe(path)
        res = archive.extract(path, dest=self.root / "out-version")
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"])

    def _zip_with_method(self, method: int, payload: bytes) -> Path:
        """Gecerli ZIP'in sikistirma yontemini degistirir (veri uydurma)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("a.txt", payload)
        data = bytearray(buf.getvalue())
        data[8:10] = method.to_bytes(2, "little")       # yerel baslik
        cd = data.rfind(b"PK\x01\x02")
        data[cd + 10:cd + 12] = method.to_bytes(2, "little")
        path = self.root / f"method-{method}.zip"
        path.write_bytes(bytes(data))
        return path

    def test_corrupt_member_reports_extract_error(self):
        """Bozuk deflate uye cikarmayi dusurmez; hata satiri olur."""
        path = self._zip_with_method(zipfile.ZIP_DEFLATED, b"\xff" * 16)
        out = self.root / "out-deflate"
        res = archive.extract(path, dest=out)
        self.assertFalse(res["ok"])
        self.assertEqual(res["extracted"], 0)
        self.assertTrue(any("a.txt" in err for err in res["errors"]),
                        res["errors"])
        self.assertFalse((out / "a.txt").exists())
        self.assertFalse((out / "a.txt.part").exists())

    def test_unsupported_method_reports_extract_error(self):
        """Desteklenmeyen sikistirma yontemi (99) hata satiri olur."""
        path = self._zip_with_method(99, b"x" * 16)
        out = self.root / "out-method"
        res = archive.extract(path, dest=out)
        self.assertFalse(res["ok"])
        self.assertEqual(res["extracted"], 0)
        self.assertTrue(any("a.txt" in err for err in res["errors"]),
                        res["errors"])

    def test_corrupt_nested_member_reports_extract_error(self):
        """'inner.zip!/a.txt' yolu da (akis) hata satiri uretir, cokmez."""
        inner = self._zip_with_method(zipfile.ZIP_DEFLATED,
                                      b"\xff" * 16).read_bytes()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("a.txt", b"a")
            zf.writestr("inner.zip", inner)
        path = self.root / "outer.zip"
        path.write_bytes(buf.getvalue())
        out = self.root / "out-nested"
        res = archive.extract(path, members=["inner.zip!/a.txt"], dest=out)
        self.assertFalse(res["ok"])
        self.assertTrue(any("inner.zip!/a.txt" in err
                            for err in res["errors"]), res["errors"])


if __name__ == "__main__":
    unittest.main()
