"""HLS (m3u8) indirici: varyant secimi + paylasilan segment derleyici.

Not: Canli yayin (ENDLIST'siz) ve DRM'li icerik desteklenmez.
"""
from __future__ import annotations

import re
import threading
import urllib.parse
from pathlib import Path

from ..ratelimit import TokenBucket
from . import httpcli, segments


class HlsError(Exception):
    pass


def parse_playlist(text: str, base_url: str):
    """(segments, init_url, is_master, variants) dondurur."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    variants, segs, init_url = [], [], None
    is_master = False
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            is_master = True
            bw = re.search(r"BANDWIDTH=(\d+)", line)
            uri = lines[i + 1] if i + 1 < len(lines) \
                and not lines[i + 1].startswith("#") else ""
            variants.append({"url": urllib.parse.urljoin(base_url, uri),
                             "bandwidth": int(bw.group(1)) if bw else 0})
        elif line.startswith("#EXT-X-MAP:URI="):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                init_url = urllib.parse.urljoin(base_url, m.group(1))
        elif not line.startswith("#"):
            segs.append(urllib.parse.urljoin(base_url, line))
    return segs, init_url, is_master, variants


def fetch_text(url: str, opt: httpcli.DlOptions) -> str:
    opener = httpcli._make_opener(opt)
    with opener.open(httpcli.request_of(url, opt), timeout=opt.timeout) as r:
        return r.read().decode("utf-8", "replace")


def download_hls(url: str, opt: httpcli.DlOptions, fd: int, meta: dict,
                 stop_event: threading.Event, bucket: TokenBucket,
                 progress_cb, state, meta_path: Path | None = None) -> None:
    text = fetch_text(url, opt)
    segs, init_url, is_master, variants = parse_playlist(text, url)
    if is_master:
        if not variants:
            raise HlsError("master listesinde varyant yok")
        best = max(variants, key=lambda v: v["bandwidth"])
        state["variant_url"] = best["url"]
        text = fetch_text(best["url"], opt)
        segs, init_url, _, _ = parse_playlist(text, best["url"])
    if not segs:
        raise HlsError("segment bulunamadi")
    if "#EXT-X-ENDLIST" not in text:
        raise HlsError("canli yayin akisi desteklenmiyor")

    meta["kind"] = "hls"
    meta.setdefault("url", url)
    segments.assemble(opt, fd, meta, stop_event, bucket, progress_cb,
                      meta_path, init_url, segs)
