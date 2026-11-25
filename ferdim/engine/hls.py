"""HLS (m3u8) indirici: varyant secimi + paylasilan segment derleyici.

Desteklenen medya ozellikleri: EXT-X-BYTERANGE (tek dosyada alt araliklar)
ve EXT-X-KEY METHOD=AES-128 (anahtar rotasyonu dahil). `cryptography`
yalnizca cozme aninda, tembel import edilir; yoksa is `hls_crypto_missing`
ile biter. SAMPLE-AES/DRM (`hls_drm`) desteklenmez. Anahtarlar yalnizca
bellekte tutulur: diske/meta'ya/loga yazilmaz.

Not: Canli yayin (ENDLIST'siz) desteklenmez.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path

from ..ratelimit import TokenBucket
from . import httpcli, segments

_ATTR_RE = re.compile(r'([A-Za-z0-9-]+)=("[^"]*"|[^,]*)')
MAX_SEGMENTS = 20_000     # tek oynatma listesi icin segment ust siniri
MAX_KEYS = 4_096          # tek oynatma listesi icin farkli anahtar URI siniri
_MAX_DIGITS = 18          # int() tasmasi/DoS korumasi


class HlsError(segments.StreamError):
    pass


def _num(text, what: str = "sayi") -> int:
    """Manifest sayisini guvenle cevirir (uzun rakam dizisi reddedilir)."""
    s = str(text if text is not None else "").strip()
    digits = s.lstrip("0") or "0"
    if not s.isdigit() or len(digits) > _MAX_DIGITS:
        raise HlsError(f"gecersiz manifest {what}: {s[:32]}",
                       "manifest_number")
    return int(s)


@dataclass
class HlsSegment:
    """Bir m3u8 medya segmenti: URL + istege bagli alt aralik ve anahtar."""
    url: str
    seq: int = 0
    start: int | None = None
    length: int | None = None
    key_uri: str | None = None
    iv: bytes | None = None


@dataclass
class Playlist:
    segments: list[HlsSegment] = field(default_factory=list)
    init_url: str | None = None
    is_master: bool = False
    variants: list[dict] = field(default_factory=list)
    endlist: bool = False


def _attrs(line: str) -> dict:
    return {k.upper(): v.strip().strip('"')
            for k, v in _ATTR_RE.findall(line)}


def _parse_key(line: str, base_url: str):
    """EXT-X-KEY -> (anahtar URL, IV) ya da METHOD=NONE icin None."""
    a = _attrs(line)
    method = (a.get("METHOD") or "").upper()
    if method == "NONE":
        return None
    if method != "AES-128":
        raise HlsError(
            f"desteklenmeyen HLS sifreleme yontemi: {method or '?'}",
            "hls_drm")
    kf = (a.get("KEYFORMAT") or "identity").strip()
    if kf.lower() != "identity":
        raise HlsError("DRM anahtar bicimi desteklenmiyor (KEYFORMAT)",
                       "hls_drm")
    uri = a.get("URI") or ""
    if not uri:
        raise HlsError("EXT-X-KEY URI eksik", "hls_drm")
    iv = None
    raw = (a.get("IV") or "").strip()
    if raw:
        if raw.lower().startswith("0x"):
            raw = raw[2:]
        if not re.fullmatch(r"[0-9A-Fa-f]{1,32}", raw):
            raise HlsError("gecersiz EXT-X-KEY IV", "manifest_number")
        try:
            iv = int(raw, 16).to_bytes(16, "big")
        except (ValueError, OverflowError) as e:
            raise HlsError("gecersiz EXT-X-KEY IV", "manifest_number") from e
    return segments.resolve_url(base_url, uri, exc=HlsError), iv


def _parse_byterange(line: str):
    """EXT-X-BYTERANGE:<n>[@<o>] -> (n, o|None); bozuksa HlsError."""
    raw = line.split(":", 1)[1].strip() if ":" in line else ""
    m = re.match(r"^(\d+)(?:@(\d+))?$", raw)
    if not m:
        raise HlsError("gecersiz EXT-X-BYTERANGE", "manifest_number")
    n = _num(m.group(1), "byterange")
    if n <= 0:
        raise HlsError("gecersiz EXT-X-BYTERANGE", "manifest_number")
    off = _num(m.group(2), "byterange") if m.group(2) is not None else None
    return n, off


def parse_media(text: str, base_url: str) -> Playlist:
    """m3u8'i tam olarak ayristirir (segment araligi, anahtar, sira)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    pl = Playlist(endlist=any(l.startswith("#EXT-X-ENDLIST") for l in lines))
    media_seq = 0
    cur_key = None                 # (uri, iv) ya da NONE sonrasi None
    pending_range = None           # (n, o|None)
    last_end: dict[str, int] = {}  # ayni URI'nin son alt aralik sonu
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            raw = line.split(":", 1)[1].strip()
            media_seq = _num(raw, "medya sirasi") if raw.isdigit() else 0
        elif line.startswith("#EXT-X-STREAM-INF"):
            pl.is_master = True
            bw = re.search(r"BANDWIDTH=(\d+)", line)
            uri = lines[i + 1] if i + 1 < len(lines) \
                and not lines[i + 1].startswith("#") else ""
            pl.variants.append({
                "url": segments.resolve_url(base_url, uri, exc=HlsError),
                "bandwidth": _num(bw.group(1), "bandwidth") if bw else 0})
        elif line.startswith("#EXT-X-MAP:"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                pl.init_url = segments.resolve_url(base_url, m.group(1),
                                                   exc=HlsError)
        elif line.startswith("#EXT-X-KEY:"):
            cur_key = _parse_key(line, base_url)
        elif line.startswith("#EXT-X-BYTERANGE:"):
            pending_range = _parse_byterange(line)
        elif not line.startswith("#"):
            url = segments.resolve_url(base_url, line, exc=HlsError)
            start = length = None
            if pending_range is not None:
                n, off = pending_range
                start = off if off is not None else last_end.get(url, 0)
                length = n
                last_end[url] = start + n
                pending_range = None
            pl.segments.append(HlsSegment(
                url=url, seq=media_seq + len(pl.segments),
                start=start, length=length,
                key_uri=cur_key[0] if cur_key else None,
                iv=cur_key[1] if cur_key else None))
            if len(pl.segments) > MAX_SEGMENTS:
                raise HlsError("segment sayisi ust sinirin uzerinde",
                               "stream_limit")
    return pl


def parse_playlist(text: str, base_url: str):
    """(segments, init_url, is_master, variants) dondurur (eski imza).

    Zengin bilgi (aralik/anahtar/sira) icin `parse_media` kullanilir.
    """
    pl = parse_media(text, base_url)
    return [s.url for s in pl.segments], pl.init_url, pl.is_master, pl.variants


def fetch_text(url: str, opt: httpcli.DlOptions) -> str:
    """Sinirli boyutta playlist metni (manifest_too_large korumasi)."""
    return segments.fetch_manifest(url, opt)


def _fetch_key_once(url: str, opt: httpcli.DlOptions) -> bytes:
    scoped = segments.scoped_options(opt, url)
    opener = httpcli._make_opener(scoped)
    with opener.open(httpcli.request_of(url, scoped),
                     timeout=opt.timeout) as r:
        data = r.read(17)
    if len(data) != 16:
        raise HlsError("HLS anahtari 16 bayt olmali", "hls_key_invalid")
    return data


def fetch_key(url: str, opt: httpcli.DlOptions,
              stop_event: threading.Event | None = None) -> bytes | None:
    """AES-128 anahtarini ceker (16 bayt), gecici hatalarda geri cekilmeyle.

    Anahtar yalnizca bellekte kalir. `stop_event` verilirse bekleme ve
    denemeler durdurmayi gorur (durunca None). Kalici hata (404/auth/16
    bayt degil) hemen yukselir; gecici hata segmentlerle ayni geri cekilmeyi
    kullanir."""
    retries = max(0, int(getattr(opt, "retries", 5)))
    attempt = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            return None
        try:
            return _fetch_key_once(url, opt)
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError):
                e.close()
            if stop_event is not None and stop_event.is_set():
                return None
            if not segments.is_transient(e) or attempt >= retries:
                raise
            delay = segments.retry_delay(attempt, e)
            attempt += 1
            if stop_event is not None and stop_event.wait(delay):
                return None


def _entries(pl: Playlist, opt: httpcli.DlOptions,
             stop_event: threading.Event | None = None) -> list | None:
    """Playlist segmentlerini assembler girdilerine cevirir.

    Anahtarlar URI basina BIR kez cekilir (rotasyon olsa da ayni URI tekrar
    inmez). IV yoksa segmentin medya sira numarasi 16 bayt buyuk-endian
    olarak kullanilir (RFC 8216). Distinct anahtar URI sayisi sinirlidir;
    durdurma gelirse None doner."""
    uris = sorted({s.key_uri for s in pl.segments if s.key_uri})
    if len(uris) > MAX_KEYS:
        raise HlsError("anahtar URI sayisi ust sinirin uzerinde",
                       "stream_limit")
    if uris and not segments.aes_available():
        raise HlsError("sifreli HLS icin cryptography gerekli",
                       "hls_crypto_missing")
    keys = {}
    for u in uris:
        key = fetch_key(u, opt, stop_event)
        if key is None:                 # duraklat/iptal
            return None
        keys[u] = key
    out = []
    for s in pl.segments:
        key = keys.get(s.key_uri) if s.key_uri else None
        iv = s.iv
        if key is not None and iv is None:
            iv = int(s.seq).to_bytes(16, "big")
        out.append(segments.Segment(url=s.url, start=s.start, length=s.length,
                                    key=key, iv=iv))
    return out


def download_hls(url: str, opt: httpcli.DlOptions, fd: int, meta: dict,
                 stop_event: threading.Event, bucket: TokenBucket,
                 progress_cb, state, meta_path: Path | None = None,
                 *, store=None, cap: int = segments.SEG_MAX) -> None:
    t0 = time.monotonic()
    pl = parse_media(fetch_text(url, opt), url)
    rtt = time.monotonic() - t0      # segment havuzunun RTT bandi (ek istek yok)
    if pl.is_master:
        if not pl.variants:
            raise HlsError("master listesinde varyant yok")
        best = max(pl.variants, key=lambda v: v["bandwidth"])
        state["variant_url"] = best["url"]
        t0 = time.monotonic()
        pl = parse_media(
            fetch_text(best["url"], segments.scoped_options(opt, best["url"])),
            best["url"])
        rtt = time.monotonic() - t0  # segment listesini veren istek olculur
    if not pl.segments:
        raise HlsError("segment bulunamadi")
    if not pl.endlist:
        raise HlsError("canli yayin akisi desteklenmiyor")

    entries = _entries(pl, opt, stop_event)
    if entries is None:                 # duraklat/iptal
        return
    meta["kind"] = "hls"
    meta.setdefault("url", url)
    segments.assemble(opt, fd, meta, stop_event, bucket, progress_cb,
                      meta_path, pl.init_url, entries, store=store, cap=cap,
                      rtt=rtt)
