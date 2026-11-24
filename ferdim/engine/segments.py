"""HLS ve DASH'in paylastigi segment derleyici.

Segmentler paralel cekilir, gecici dosyalarda bekletilir, sonra SIRAYLA
ciktiya yazilir; segment bazli resume meta uzerinden yurur.

Havuz isci sayisi sabit degil: ana makine icin ogrenilmis `seg_workers`
degeri baslangic noktasidir (yoksa segment sayisinin yarisi, en cok
SEG_MAX). Bitiste/duraklamada yeterli segment indiyse kullanilan sayi
profil deposuna bildirilir; 429/503 gorulduyse yarisi bildirilir.
"""
from __future__ import annotations

import os
import threading
import time
import urllib.error
from pathlib import Path

from ..ratelimit import TokenBucket
from . import httpcli

SEG_MAX = 8               # segment havuzu tavani (aralik islerinden bagimsiz)
SEG_LEARN_MIN = 4         # ogrenmek icin en az bu kadar segment inmeli


def pool_size(n_segments: int, seed: int = 0, cap: int = SEG_MAX) -> int:
    """Havuz isci sayisi: ogrenilmis tohum, segment sayisi ve tavan.

    Tohum yoksa eski davranis korunur (segment sayisinin yarisi). Tohum
    daima 1..SEG_MAX ve dis tavan (birlikte yasam) icine kirpilir; segment
    sayisindan fazla isci acilmaz.
    """
    try:
        cap = max(1, min(SEG_MAX, int(cap or SEG_MAX)))
        n = max(1, int(n_segments))
    except (TypeError, ValueError):
        return 1
    try:
        want = int(seed or 0)
    except (TypeError, ValueError):
        want = 0               # bozuk tohum = soguk baslangic
    return max(1, min(want or max(1, n // 2), cap, n))


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


def _learn(store, url: str, workers: int, fetched: int, throttled: bool,
           rtt: float) -> None:
    """Havuz olcumunu ana makine profiline bildirir (hatalar yutulur).

    Yalniz yeterli segment inen kosu ogrenilir: tek segmentlik sansli bir
    kosu havuzu yanlis yere cekmesin. 429/503 gorulduyse yarisi bildirilir
    (sunucunun kabul ettigi sayi).
    """
    if store is None or not url or fetched < SEG_LEARN_MIN:
        return
    try:
        store.record(url, seg_workers=workers if not throttled
                     else max(1, workers // 2),
                     throttled=throttled, rtt=rtt)
    except Exception:
        pass


def assemble(opt: httpcli.DlOptions, fd: int, meta: dict,
             stop_event: threading.Event, bucket: TokenBucket,
             progress_cb, meta_path: Path, init_url: str | None,
             seg_urls: list[str], *, store=None, cap: int = SEG_MAX,
             rtt: float = 0.0) -> None:
    """init_url + seg_urls sirali birlesimi. meta icinde: done_segments,
    cursor, init_done. progress_cb(byte) cekim sirasinda cagrilir.

    store verilirse (HostProfiles) havuz isci sayisi ogrenilir: tohum
    profilden okunur, kosu sonunda ya da duraklamada geri bildirilir.
    rtt: bu kosudaki manifest yanit suresi (RTT bandi icin)."""
    done: set[int] = set(int(i) for i in meta.get("done_segments", []))
    meta["segments_total"] = len(seg_urls)
    segs_dir = Path(str(meta_path) + ".segs")
    segs_dir.mkdir(parents=True, exist_ok=True)

    host_url = seg_urls[0] if seg_urls else ""
    seed = 0
    if store is not None and host_url:
        try:
            seed = int((store.get(host_url) or {}).get("seg_workers") or 0)
        except Exception:
            seed = 0            # profil okunamadi: soguk baslangic
    workers = pool_size(len(seg_urls), seed, cap)

    errors: list[str] = []
    lock = threading.Lock()
    last_save = [0.0]
    fetched = [0]              # bu kosuda agdan inen segment sayisi
    throttled = [False]

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
                # 429/503: sunucu eszamanli baglantiyi kisitliyor olabilir
                if isinstance(e, urllib.error.HTTPError) \
                        and e.code in (429, 503):
                    with lock:
                        throttled[0] = True
                with lock:
                    errors.append(f"segment {idx}: {e}")
                return
            tmp = f.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(f)
            with lock:
                fetched[0] += 1
        with lock:
            if progress_cb and idx not in done:
                progress_cb(f.stat().st_size)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, range(len(seg_urls))))
    _learn(store, host_url, workers, fetched[0], throttled[0], rtt)
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
