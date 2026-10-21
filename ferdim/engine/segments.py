"""HLS ve DASH'in paylastigi segment derleyici.

Segmentler paralel cekilir, gecici dosyalarda bekletilir, sonra SIRAYLA
ciktiya yazilir; segment bazli resume meta uzerinden yurur.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from ..ratelimit import TokenBucket
from . import httpcli


def fetch_all(url: str, opt: httpcli.DlOptions, bucket: TokenBucket) -> bytes:
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


def assemble(opt: httpcli.DlOptions, fd: int, meta: dict,
             stop_event: threading.Event, bucket: TokenBucket,
             progress_cb, meta_path: Path, init_url: str | None,
             seg_urls: list[str]) -> None:
    """init_url + seg_urls sirali birlesimi. meta icinde: done_segments,
    cursor, init_done. progress_cb(byte) cekim sirasinda cagrilir."""
    done: set[int] = set(int(i) for i in meta.get("done_segments", []))
    meta["segments_total"] = len(seg_urls)
    segs_dir = Path(str(meta_path) + ".segs")
    segs_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    lock = threading.Lock()
    last_save = [0.0]

    def persist(force=False):
        import json as _j
        now = time.monotonic()
        if not force and now - last_save[0] < 2.0:
            return
        last_save[0] = now
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
                data = fetch_all(seg_urls[idx], opt, bucket)
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
    workers = min(8, max(1, len(seg_urls) // 2))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, range(len(seg_urls))))
    if stop_event.is_set():
        persist(force=True)
        return
    if errors:
        raise RuntimeError(errors[0])

    cursor = int(meta.get("cursor", 0))
    if init_url is not None and not meta.get("init_done"):
        data = fetch_all(init_url, opt, bucket)
        os.pwrite(fd, data, 0)
        cursor = len(data)
        meta["init_done"] = True
        meta["cursor"] = cursor
    for idx in range(len(seg_urls)):
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
    for f in segs_dir.glob("*.seg"):
        try:
            f.unlink()
        except OSError:
            pass
    try:
        segs_dir.rmdir()
    except OSError:
        pass
