"""Fuzz tohum korpusu (deterministik).

Her tohum ya sabit baytlardir ya da stdlib ile deterministik olarak
uretilir (zip/tar/gzip basliklarinda zaman damgasi sabitlenir). Amac:
gecerli ve kucuk ornekler; mutator bunlardan bozuk varyantlar uretir.
Boylece depoda ikili (binary) dosya tutmayiz ve korpus packaging
tarball'inda da ayni sekilde uretilir.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import stat
import tarfile
import zipfile

from ferdim import cdc

# ------------------------------------------------------------------- HLS
HLS_MEDIA = b"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-MAP:URI="init.mp4"
#EXTINF:9.9,
seg-1.ts
#EXTINF:9.9,
seg-2.ts
#EXTINF:4.2,
seg-3.ts
#EXT-X-ENDLIST
"""

HLS_MASTER = b"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2"
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720
high/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000
"""

HLS_ODD = b"""#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=000000000000000000000000001
#EXT-X-MAP:URI=""
#EXTINF:-1,
http://[::1
#EXT-X-ENDLIST
"""

# ------------------------------------------------------------------ DASH
DASH_TEMPLATE = b"""<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     profiles="urn:mpeg:dash:profile:isoff-live:2011">
  <BaseURL>v/</BaseURL>
  <Period id="p0" duration="PT30S">
    <AdaptationSet mimeType="video/mp4" segmentAlignment="true">
      <Representation id="v1" width="1280" height="720" bandwidth="2000000">
        <SegmentTemplate timescale="1000" duration="4000"
                         initialization="$RepresentationID$/init.mp4"
                         media="$RepresentationID$/$Number$.m4s"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

DASH_TIMELINE = b"""<?xml version="1.0"?>
<MPD type="static"><Period duration="PT12S">
<AdaptationSet mimeType="video/mp4"><Representation id="v" width="640"
  height="360" bandwidth="900000">
  <SegmentTemplate media="s-$Number$-$Time$.m4s" initialization="i.mp4">
    <SegmentTimeline>
      <S t="0" d="4000" r="2"/>
      <S d="4000" r="0"/>
    </SegmentTimeline>
  </SegmentTemplate>
</Representation></AdaptationSet></Period></MPD>
"""

DASH_LIST = b"""<?xml version="1.0"?>
<MPD type="static"><Period duration="PT8S">
<AdaptationSet contentType="video"><Representation id="v" height="480"
  bandwidth="1000">
  <SegmentList duration="2">
    <Initialization sourceURL="init.mp4"/>
    <SegmentURL media="s1.m4s"/>
    <SegmentURL media="s2.m4s"/>
    <SegmentURL media="s3.m4s"/>
  </SegmentList>
</Representation></AdaptationSet></Period></MPD>
"""

DASH_MULTI = b"""<?xml version="1.0"?>
<MPD type="static">
  <Period id="a"><AdaptationSet mimeType="audio/mp4">
    <Representation id="a1" bandwidth="128000" audioSamplingRate="48000">
      <SegmentTemplate media="a/$Number$" duration="2" timescale="1"
                       startNumber="1"/>
    </Representation></AdaptationSet></Period>
  <Period id="b" duration="PT6S"><AdaptationSet mimeType="video/mp4">
    <Representation id="v1" width="10" height="10" bandwidth="10">
      <SegmentTemplate media="v/$Number$" duration="2" timescale="1"
                       startNumber="7"/>
    </Representation></AdaptationSet></Period>
</MPD>
"""

# -------------------------------------------------------------- metalink
METALINK = b"""<?xml version="1.0" encoding="utf-8"?>
<metalink xmlns="urn:ietf:params:xml:ns:metalink">
  <file name="ornek.bin">
    <size>1048576</size>
    <hash type="sha256">e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855</hash>
    <hash type="md5">d41d8cd98f00b204e9800998ecf8427e</hash>
    <url preference="10">https://mirror-a.example/x/ornek.bin</url>
    <url preference="1">https://mirror-b.example/ornek.bin</url>
  </file>
  <file name="ikinci.bin">
    <url>http://mirror.example/ikinci.bin</url>
  </file>
</metalink>
"""

METALINK_ODD = b"""<?xml version="1.0"?>
<metalink><file>
<size>0</size><hash type="crc32">zz</hash>
<url preference="">ftp://x/y</url>
<url preference="2">https://x/y</url>
</file></metalink>
"""


# ---------------------------------------------------------------- merkle
def _digest(label: bytes) -> str:
    return hashlib.sha256(label).hexdigest()


def _manifest(chunks: list[tuple[int, int, str]]) -> bytes:
    return cdc.Manifest.of(chunks).dumps().encode("ascii")


MERKLE_SINGLE = _manifest([(0, 300_000, _digest(b"tek"))])
MERKLE_MULTI = _manifest([
    (0, 300_000, _digest(b"a")),
    (300_000, 1_000_000, _digest(b"b")),
    (1_300_000, 262_144, _digest(b"c")),
])
MERKLE_ODD = json.dumps({
    "v": 1, "algo": "fastcdc-sha256", "size": 300000,
    "root": _digest(b"yanlis"),
    "chunks": [[0, 300000, _digest(b"x")]],
}, separators=(",", ":")).encode("ascii")


# --------------------------------------------------------------- arsivler
def _zip(members: dict[str, bytes], compression=zipfile.ZIP_DEFLATED,
         links: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
        for name, target in (links or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            info.compress_type = compression
            zf.writestr(info, target)
    return buf.getvalue()


def _tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


_INNER = _zip({"a.txt": b"ic arsiv uyesi\n" * 40,
               "alt/b.txt": b"b" * 300}, zipfile.ZIP_DEFLATED)

ARCHIVE_ZIP = _zip({
    "a.txt": b"merhaba\n" * 20,
    "dizin/b.bin": bytes(range(256)) * 4,
    "inner.zip": _INNER,
}, zipfile.ZIP_DEFLATED)

ARCHIVE_ZIP_STORED = _zip({
    "s.txt": b"saklanan" * 50,
    "inner.zip": _INNER,
}, zipfile.ZIP_STORED, links={"bag": "a.txt"})

ARCHIVE_TAR = _tar({"a.txt": b"tar uye\n" * 10,
                    "dizin/b.bin": bytes(range(64)) * 8})

ARCHIVE_TAR_GZ = gzip.compress(ARCHIVE_TAR, mtime=0)

EMPTY_ZIP = _zip({}, zipfile.ZIP_STORED)

# ------------------------------------------------------------------ SOCKS
SOCKS_OK_V4 = b"\x05\x00" + b"\x05\x00\x00\x01" + bytes((127, 0, 0, 1)) + \
    (8080).to_bytes(2, "big")
SOCKS_OK_DOMAIN = b"\x05\x00" + b"\x05\x00\x00\x03" + b"\x0bexample.com" + \
    (443).to_bytes(2, "big")
SOCKS_AUTH = b"\x05\x02" + b"\x01\x00" + \
    b"\x05\x00\x00\x04" + bytes(16) + (0).to_bytes(2, "big")
SOCKS_REFUSED = b"\x05\x00" + b"\x05\x05\x00\x01" + bytes(4) + b"\x00\x00"
SOCKS_BAD_VER = b"\x04\x00" + b"\x05\x00\x00\x01" + bytes(4) + b"\x00\x00"


# ---------------------------------------------------------------- korpus
HLS_SEEDS = (HLS_MEDIA, HLS_MASTER, HLS_ODD)
DASH_SEEDS = (DASH_TEMPLATE, DASH_TIMELINE, DASH_LIST, DASH_MULTI)
METALINK_SEEDS = (METALINK, METALINK_ODD)
MERKLE_SEEDS = (MERKLE_SINGLE, MERKLE_MULTI, MERKLE_ODD)
ARCHIVE_SEEDS = (ARCHIVE_ZIP, ARCHIVE_ZIP_STORED, ARCHIVE_TAR,
                 ARCHIVE_TAR_GZ, EMPTY_ZIP)
SOCKS_SEEDS = (SOCKS_OK_V4, SOCKS_OK_DOMAIN, SOCKS_AUTH, SOCKS_REFUSED,
               SOCKS_BAD_VER)
