"""Fuzz hedefleri: guvenilmeyen girdiyi okuyan parser'lar ve adaptorleri.

Her hedef (Target) bir `call(data, workdir)` fonksiyonu, tohum korpusu ve
"beklenen" istisna tuple'i tasir. Beklenen listeye girmeyen her istisna
bulgudur; `known.json` yalnizca bilinen sinif kimliklerini susturur.
"""
from __future__ import annotations

import socket
import tarfile
import threading
import zipfile
from pathlib import Path

from ferdim import archive, cdc, metalink
from ferdim.engine import dash, hls, socks

from . import seeds
from .harness import FuzzTimeout, Target, exc_id, time_limit

BASE_URL = "https://example.com/video/playlist.m3u8"
MPD_URL = "https://example.com/vod/manifest.mpd"


def _text(data: bytes) -> str:
    # fetch_text ile ayni: cozulemeyen baytlar degistirilir, istisna yok
    return data.decode("utf-8", "replace")


# ------------------------------------------------------------------- HLS
def _hls_call(data: bytes, workdir: Path) -> None:
    hls.parse_playlist(_text(data), BASE_URL)


HLS = Target(
    name="hls",
    seeds=seeds.HLS_SEEDS,
    call=_hls_call,
    expected=(hls.HlsError, ValueError, UnicodeError),
    tokens=(
        b"#EXTM3U\n", b"#EXT-X-ENDLIST\n", b"#EXTINF:0,\n",
        b"#EXT-X-STREAM-INF:BANDWIDTH=999999999999999999999999999\n",
        b'#EXT-X-MAP:URI="x"\n', b'#EXT-X-MAP:URI="\n',
        b"\n", b"\r\n", b"http://[::1", b"//", b"../../", b"#",
    ),
    rewrites=(
        (rb"BANDWIDTH=[0-9]+",
         (b"BANDWIDTH=0", b"BANDWIDTH=1", b"BANDWIDTH=999999999999999999999")),
        (rb"#EXTINF:[^\n]*",
         (b"#EXTINF:0,", b"#EXTINF:-1,", b"#EXTINF:1e999,", b"#EXTINF:")),
        (rb'URI="[^"]*"',
         (b'URI="x"', b'URI=""', b'URI="../../etc/passwd"')),
        (rb"#EXT-X-STREAM-INF[^\n]*",
         (b"#EXT-X-STREAM-INF", b"#EXT-X-STREAM-INF:BANDWIDTH=1")),
        (rb"#EXT-X-ENDLIST", (b"",)),
    ),
    note="m3u8 varyant/segment listesi",
)


# ------------------------------------------------------------------ DASH
def _dash_call(data: bytes, workdir: Path) -> None:
    dash.build_segment_list(_text(data), MPD_URL)


DASH = Target(
    name="dash",
    seeds=seeds.DASH_SEEDS,
    call=_dash_call,
    expected=(dash.DashError, ValueError),
    tokens=(
        b"$Time$", b"$Number$", b"$RepresentationID$", b"$",
        b'<SegmentTimeline><S t="\\1" r="1"/></SegmentTimeline>',
        b'<S t="0" r="1000000"/>', b'duration="1" timescale="1000000"',
        b'width="99999999999999999999"', b'<BaseURL>', b"</MPD>",
        b'type="dynamic"', b"<Period", b"/><", b"PT1H", b"&#x41;",
    ),
    rewrites=(
        (rb't="[^"]*"',
         (b't="\\1"', b't="\\g<1>"', b't="0"', b't="-1"',
          b't="99999999999999999999"')),
        (rb'r="[^"]*"',
         (b'r="0"', b'r="100000"', b'r="-1"', b'r="99999999999999999999"')),
        (rb'duration="[^"]*"',
         (b'duration="0"', b'duration="1"', b'duration="1000000"',
          b'duration="-1"')),
        (rb'timescale="[^"]*"',
         (b'timescale="1"', b'timescale="1000000"', b'timescale="0"')),
        (rb'startNumber="[^"]*"',
         (b'startNumber="0"', b'startNumber="-1"',
          b'startNumber="99999999999999999999"')),
        (rb'media="[^"]*"',
         (b'media="$Time$"', b'media="s$Number$"',
          b'media="$RepresentationID$"', b'media=""')),
        (rb"<Period[^>]*>",
         (b'<Period duration="PT0S">', b'<Period duration="PT100000H">')),
        (rb'width="[^"]*"',
         (b'width="0"', b'width="99999999999999999999"')),
        (rb'type="[^"]*"', (b'type="dynamic"', b'type="static"')),
    ),
    note="MPD SegmentTemplate/SegmentList",
)


# --------------------------------------------------------------- metalink
def _metalink_call(data: bytes, workdir: Path) -> None:
    metalink.parse(_text(data))


METALINK = Target(
    name="metalink",
    seeds=seeds.METALINK_SEEDS,
    call=_metalink_call,
    expected=(ValueError,),
    tokens=(
        b"<file", b'<url preference="', b">", b"</file>", b"<hash type=",
        b"&amp;", b"&#x41;", b"&nbsp;", b"<![CDATA[", b"]]>", b"\x00",
        b' preference="999999999999999999999"',
    ),
    rewrites=(
        (rb'preference="[^"]*"',
         (b'preference=""', b'preference="0"', b'preference="-1"',
          b'preference="99999999999999999999"')),
        (rb"<size>[^<]*</size>",
         (b"<size>0</size>", b"<size>-5</size>",
          b"<size>99999999999999999999</size>", b"<size></size>")),
        (rb'<hash type="[^"]*"',
         (b'<hash type=""', b'<hash type="sha256"',
          b'<hash type="' + b"x" * 64 + b'"')),
        (rb"<url>[^<]*</url>",
         (b"<url></url>", b"<url>https://x</url>",
          b"<url>" + b"a" * 2000 + b"</url>")),
        (rb"<file[^>]*>", (b"<file>", b'<file name="">', b'<file name="' +
                          b"n" * 500 + b'">')),
    ),
    note="RFC 5854 metalink /.meta4",
)


# ----------------------------------------------------------- merkle bildirim
def _merkle_call(data: bytes, workdir: Path) -> None:
    cdc.Manifest.parse(_text(data))


MERKLE = Target(
    name="merkle",
    seeds=seeds.MERKLE_SEEDS,
    call=_merkle_call,
    expected=(ValueError,),
    tokens=(
        b'"v":1', b'"algo":"fastcdc-sha256"', b'"chunks":[]', b'"size":0',
        b"null", b"true", b"-1", b"1e999", b'"\\u0000"', b"[" * 64,
        b'"root":"' + b"0" * 64 + b'"', b"[[0,1,\"" + b"0" * 64 + b'"]]',
    ),
    rewrites=(
        (rb'"size":\s*-?[0-9]+',
         (b'"size":0', b'"size":-1', b'"size":99999999999999999999',
          b'"size":1.5')),
        (rb'"chunks":\s*\[',
         (b'"chunks":' + b"[" * 4000 + b"]" * 4000,
          b'"chunks":' + b"[" * 4000,
          b'"chunks":[[]', b'"chunks":[["a","b","c"]]')),
        (rb'"v":\s*[0-9]+', (b'"v":0', b'"v":2', b'"v":1')),
        (rb'"algo":\s*"[^"]*"', (b'"algo":"x"', b'"algo":null')),
        (rb'"root":\s*"[^"]*"',
         (b'"root":"' + b"0" * 64 + b'"', b'"root":null',
          b'"root":"' + b"g" * 64 + b'"')),
    ),
    note=".ferdim-merkle JSON bildirimi",
)


# --------------------------------------------------------------- arsivler
def _archive_call(data: bytes, workdir: Path) -> None:
    path = workdir / "case.zip"
    path.write_bytes(data)
    archive.describe(path)


ARCHIVE = Target(
    name="archive",
    seeds=seeds.ARCHIVE_SEEDS,
    call=_archive_call,
    expected=(archive.ArchiveError, OSError, EOFError, ValueError,
              zipfile.BadZipFile, tarfile.TarError),
    tokens=(
        b"PK\x03\x04", b"PK\x01\x02", b"PK\x05\x06", b"PK\x07\x08",
        b"ustar\x0000", b"\x1f\x8b\x08", b"inner.zip", b"../", b"/etc/",
        b"\x00" * 8, b"BZh", b"\xfd7zXZ\x00",
    ),
    note="ZIP/TAR listeleme (ic ice uyeler dahil)",
)


# ------------------------------------------------------------------ SOCKS
_PROXY = socks.Proxy("socks5", "proxy.invalid", 1080)


def _socks_call(data: bytes, workdir: Path) -> None:
    client, server = socket.socketpair()

    def reply() -> None:
        try:
            server.sendall(data)
        except OSError:
            pass
        finally:
            try:
                server.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            finally:
                server.close()

    thread = threading.Thread(target=reply, daemon=True)
    client.settimeout(1.0)
    thread.start()
    try:
        socks._handshake(client, _PROXY, "127.0.0.1", 443)
    finally:
        client.close()
        thread.join(2.0)


SOCKS = Target(
    name="socks",
    seeds=seeds.SOCKS_SEEDS,
    call=_socks_call,
    expected=(OSError,),   # SocksError, socket.timeout, reset hepsi OSError
    tokens=(
        b"\x05", b"\x04", b"\x01", b"\x00", b"\xff", b"\x02",
        b"\x05\x00", b"\x05\x02", b"\x05\x00\x00\x01",
        b"\x05\x00\x00\x03", b"\x05\x00\x00\x04", b"\x05\xff\x00\x01",
        bytes(range(2, 40)),
    ),
    rewrites=(
        (rb"\x05[\x00-\xff]",
         (b"\x05\x00", b"\x05\x02", b"\x05\xff", b"\x05\x01")),
        (rb"\x05\x00\x00[\x00-\xff]",
         (b"\x05\x00\x00\x01", b"\x05\x00\x00\x03", b"\x05\x00\x00\x04",
          b"\x05\x00\x00\x7f")),
    ),
    note="RFC 1928/1929 sunucu yaniti ayristirma",
)


TARGETS: dict[str, Target] = {
    t.name: t for t in (HLS, DASH, METALINK, MERKLE, ARCHIVE, SOCKS)
}


# test_fuzz_smoke icin: hedef basina sabit girdi sayisi (deterministik
# sira). Sure yerine sayac kullanilir ki ayni makinede ve CI'da ayni
# girdiler kosulsun. Toplam ~5 s (dash: asilma sinifi 2 s + 0.5 s tekrar).
SMOKE_ITERATIONS = {
    "hls": 600,
    "dash": 300,
    "metalink": 600,
    "merkle": 600,
    "archive": 500,
    "socks": 600,
}

SMOKE_SEED = 20251124


# ------------------------------------------------------- XML varlik sismesi
def _xml_bomb(levels: int, body: bytes) -> bytes:
    """Billion-laughs benzeri belge; her seviye 10 kat genisler.

    6 seviye genisletilirse ~9 MB'lik tek bir metin olusur. Saglam bir
    ayristirici bunu reddeder (expat 2.6+ yukseltme siniri) ya da cikti
    sinirli kalir; test_fuzz_smoke ikisini de kabul eder, kabul edilen
    sinirsiz sismeyi yakalar.
    """
    parts = [b'<?xml version="1.0"?>\n<!DOCTYPE lolz [\n'
             b'<!ENTITY lol "http://a/">\n']
    prev = b"lol"
    for i in range(1, levels + 1):
        name = b"lol%d" % i
        parts.append(b'<!ENTITY %s "%s">\n'
                     % (name, (b"&%s;" % prev) * 10))
        prev = name
    parts.append(b']>\n' + body % prev)
    return b"".join(parts)


def xml_bomb_doc(levels: int = 6) -> bytes:
    """Metalink yolu: genisletme <url> metninde."""
    return _xml_bomb(levels,
                     b'<metalink><file name="b"><url>&%s;</url></file>'
                     b"</metalink>\n")


def xml_bomb_mpd(levels: int = 6) -> bytes:
    """DASH yolu: genisletme <BaseURL> metninde (ayni ET ayristirici)."""
    return _xml_bomb(levels,
                     b'<MPD type="static"><BaseURL>&%s;</BaseURL>'
                     b'<Period duration="PT2S"><AdaptationSet '
                     b'mimeType="video/mp4"><Representation id="v" '
                     b'width="1" height="1" bandwidth="1">'
                     b'<SegmentTemplate media="s$Number$" duration="1" '
                     b'timescale="1"/></Representation></AdaptationSet>'
                     b"</Period></MPD>\n")


def seeds_clean(target: Target, workdir: Path, timeout: float = 2.0) -> None:
    """Tohumlarin kendisi beklenmeyen istisna/sure asimi uretmemeli."""
    for seed in target.seeds:
        try:
            with time_limit(timeout):
                target.call(seed, workdir)
        except target.expected:
            continue
        except FuzzTimeout as exc:
            raise AssertionError(f"{target.name}: tohum sure siniri asildi: "
                                 f"{exc}") from None
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"{target.name}: tohum beklenmeyen istisna: "
                f"{exc_id(exc)}: {exc}") from exc
