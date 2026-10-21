"""HLS (m3u8) indirici: varyant secimi + paralel segment cekimi + sirali birlesim.

Not: Canli yayin (ENDLIST'siz) ve DRM'li icerik desteklenmez.
Segmentler paralel cekilir, gecici dosyalara yazilir, sonra sirayla ciktiya
eklenir; boylece devami (resume) segment bazinda calisir.
"""
from __future__ import annotations

import os
import re
import threading
import urllib.parse
from pathlib import Path

from ..ratelimit import TokenBucket
from . import httpcli


class HlsError(Exception):
    pass


def parse_playlist(text: str, base_url: str):
    """(segments, init_url, is_master, variants) dondurur."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    variants, segments, init_url = [], [], None
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
            segments.append(urllib.parse.urljoin(base_url, line))
    return segments, init_url, is_master, variants


def fetch_text(url: str, opt: httpcli.DlOptions) -> str:
    opener = httpcli._make_opener(opt)
    with opener.open(httpcli.request_of(url, opt), timeout=opt.timeout) as r:
        return r.read().decode("utf-8", "replace")


def download_hls(url: str, opt: httpcli.DlOptions, fd: int, meta: dict,
                 stop_event: threading.Event, bucket: TokenBucket,
                 progress_cb, state, meta_path=None) -> None:
    text = fetch_text(url, opt)
    segments, init_url, is_master, variants = parse_playlist(text, url)
    if is_master:
        if not variants:
            raise HlsError("master listesinde varyant yok")
        best = max(variants, key=lambda v: v["bandwidth"])
        state["variant_url"] = best["url"]
        text = fetch_text(best["url"], opt)
        segments, init_url, _, _ = parse_playlist(text, best["url"])
    if not segments:
        raise HlsError("segment bulunamadi")
    if "#EXT-X-ENDLIST" not in text:
        raise HlsError("canli yayin akisi desteklenmiyor")

    meta["kind"] = "hls"
    meta["url"] = url
    meta["segments_total"] = len(segments)
    done: set[int] = set(int(i) for i in meta.get("done_segments", []))
    segs_dir = Path(str(meta_path) + ".segs") if meta_path else None
    if segs_dir is None:
        import tempfile
        segs_dir = Path(tempfile.mkdtemp(prefix="ferdim-hls-"))
    segs_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    lock = threading.Lock()
    last_save = [0.0]

    def persist(force=False):
        import time as _t
        if meta_path is None:
            return
        now = _t.monotonic()
        if not force and now - last_save[0] < 2.0:
            return
        last_save[0] = now
        import json as _j
        tmp = Path(str(meta_path) + ".tmp")
        tmp.write_text(_j.dumps(meta, ensure_ascii=False), "utf-8")
        tmp.replace(meta_path)

    def seg_file(idx: int) -> Path:
        return segs_dir / f"{idx:06d}.seg"

    def work(idx: int):
        if stop_event.is_set() or errors:
            return
        f = seg_file(idx)
        if not f.exists():
            try:
                data = _fetch_all(segments[idx], opt, bucket)
            except Exception as e:
                with lock:
                    errors.append(f"segment {idx}: {e}")
                return
            tmp = f.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(f)
        with lock:
            if progress_cb and idx not in done:
                progress_cb(f.stat().st_size)

    from concurrent.futures import ThreadPoolExecutor
    workers = min(8, max(1, len(segments) // 2))  # segment sayisina gore olcekli
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, range(len(segments))))
    if stop_event.is_set():
        persist(force=True)
        return
    if errors:
        raise HlsError(errors[0])

    # sirali birlestirme: init segment + tum parcalar
    cursor = int(meta.get("cursor", 0))
    if init_url is not None and not meta.get("init_done"):
        data = _fetch_all(init_url, opt, bucket)
        os.pwrite(fd, data, 0)
        cursor = len(data)
        meta["init_done"] = True
        meta["cursor"] = cursor
    for idx in range(len(segments)):
        if idx in done:
            continue
        data = seg_file(idx).read_bytes()
        os.pwrite(fd, data, cursor)
        cursor += len(data)
        done.add(idx)
        meta["done_segments"] = sorted(done)
        meta["cursor"] = cursor
        persist()
    os.ftruncate(fd, cursor)
    meta["tail"] = cursor
    persist(force=True)
    if progress_cb:
        progress_cb(0)
    # temizlik
    for f in segs_dir.glob("*.seg"):
        try:
            f.unlink()
        except OSError:
            pass
    try:
        segs_dir.rmdir()
    except OSError:
        pass


def _fetch_all(url: str, opt: httpcli.DlOptions, bucket: TokenBucket) -> bytes:
    opener = httpcli._make_opener(opt)
    buf = bytearray()
    with opener.open(httpcli.request_of(url, opt), timeout=opt.timeout) as r:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            bucket.take(len(chunk))
            buf += chunk
    return bytes(buf)
