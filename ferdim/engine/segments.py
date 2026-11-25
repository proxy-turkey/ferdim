"""HLS ve DASH'in paylastigi segment derleyici.

Segmentler paralel cekilir, gecici dosyalarda bekletilir, sonra SIRAYLA
ciktiya yazilir; segment bazli resume meta uzerinden yurur.

Havuz isci sayisi sabit degil: ana makine icin ogrenilmis `seg_workers`
degeri baslangic noktasidir (yoksa segment sayisinin yarisi, en cok
SEG_MAX). Bitiste/duraklamada yeterli segment indiyse kullanilan sayi
profil deposuna bildirilir; 429/503 gorulduyse yarisi bildirilir.
"""
from __future__ import annotations

import http.client
import os
import re
import ssl
import threading
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path

from .. import util
from ..ratelimit import TokenBucket
from . import httpcli

SEG_MAX = 8               # segment havuzu tavani (aralik islerinden bagimsiz)
SEG_LEARN_MIN = 4         # ogrenmek icin en az bu kadar segment inmeli
MANIFEST_MAX = 8 << 20    # HLS/DASH manifest ust siniri (bellek korumasi)


class StreamError(Exception):
    """Akis (HLS/DASH) hatalari; `code` arayuzler icin kararli hata kodudur."""

    def __init__(self, msg: str, code: str = ""):
        super().__init__(msg)
        self.code = code


@dataclass(frozen=True)
class Segment:
    """Bir segment istegi. `start`/`length` varsa yalniz o alt aralik iner
    (HLS EXT-X-BYTERANGE); `key` varsa govde AES-128-CBC ile cozulur.

    Anahtar/IV yalnizca BELLEKTE tutulur: diske, meta'ya ya da loga yazilmaz.
    """
    url: str
    start: int | None = None
    length: int | None = None
    key: bytes | None = None
    iv: bytes | None = None


def _as_segment(item) -> Segment:
    return item if isinstance(item, Segment) else Segment(str(item))


def aes_available() -> bool:
    """`cryptography` ile AES-128 cozme mumkun mu? (tembel import)."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher  # noqa: F401
        return True
    except Exception:
        return False


def _decrypt_aes128(data: bytes, key: bytes, iv: bytes | None) -> bytes:
    """AES-128-CBC cozer ve PKCS7 dolgusunu atar (tembel import)."""
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes)
    except Exception as e:      # kurulu degil: kalici, tekrar denemeye deger
        raise StreamError("sifreli HLS icin cryptography gerekli",
                          "hls_crypto_missing") from e
    if key is None or len(key) != 16:
        raise StreamError("HLS AES-128 anahtari 16 bayt olmali",
                          "hls_key_invalid")
    if iv is None or len(iv) != 16:
        raise StreamError("HLS AES-128 IV 16 bayt olmali", "hls_key_invalid")
    if not data or len(data) % 16:
        raise StreamError("sifreli segment blok hizali degil", "hls_decrypt")
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = dec.update(data) + dec.finalize()
    try:
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(plain) + unpadder.finalize()
    except Exception as e:
        raise StreamError("segment sifresi cozulemedi", "hls_decrypt") from e


def fetch_manifest(url: str, opt: httpcli.DlOptions,
                   limit: int | None = None) -> str:
    """Sinirli boyutta manifest metni ceker (HLS m3u8 / DASH MPD).

    Sinir asilirsa `manifest_too_large` koduyla StreamError: sisman ya da
    sonsuz gorunen bir govde bellegi sisiremez. h3 seciliyse ayni
    `_make_opener` yolundan gecer.
    """
    cap = MANIFEST_MAX if limit is None else int(limit)
    opener = httpcli._make_opener(opt)
    with opener.open(httpcli.request_of(url, opt),
                     timeout=opt.timeout) as r:
        data = r.read(cap + 1)
    if len(data) > cap:
        raise StreamError("manifest boyutu ust sinirin uzerinde",
                          "manifest_too_large")
    return data.decode("utf-8", "replace")


def temp_dir(meta_path) -> Path:
    """Segment gecici klasoru: meta dosyasinin yaninda (ayni dosya sistemi)."""
    return Path(str(meta_path) + ".segs")


def cleanup(meta_path) -> None:
    """Yarim segment dosyalarini ve `.segs` klasorunu siler.

    Iptal/kaldirmada kosulsuz cagrilir; hata durumunda cagrilmaz (yarim
    segmentler devam/restart ile yeniden kullanilir)."""
    d = temp_dir(meta_path)
    try:
        for f in d.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        d.rmdir()
    except OSError:
        pass


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


def _read_body(r, bucket: TokenBucket,
               stop: threading.Event | None) -> bytes | None:
    """Yanit govdesini hiz kovasindan gecirerek okur; durunca None."""
    buf = bytearray()
    while True:
        if stop is not None and stop.is_set():
            return None
        chunk = r.read(65536)
        if not chunk:
            break
        if not bucket.take(len(chunk), stop):
            return None
        buf += chunk
    return bytes(buf)


def fetch_all(url: str, opt: httpcli.DlOptions, bucket: TokenBucket,
              stop: threading.Event | None = None) -> bytes | None:
    """Tek akisla tum govdeyi ceker. `stop` verilirse hiz kovasinda bekleme
    ve her okuma blogu durdurmayi gorur: durunca None doner (cagiran yarim
    veriyi YAZMAZ; boylece duraklatma hiz limiti altinda da senkron olur)."""
    opener = httpcli._make_opener(opt)
    with opener.open(httpcli.request_of(url, opt), timeout=opt.timeout) as r:
        return _read_body(r, bucket, stop)


_CR_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")


def fetch_range(url: str, opt: httpcli.DlOptions, bucket: TokenBucket,
                start: int, length: int,
                stop: threading.Event | None = None) -> bytes | None:
    """`url` uzerinden YALNIZ [start, start+length) alt araligini ceker.

    HLS EXT-X-BYTERANGE icin: sunucu Range'i yok sayip 200 dondururse ya da
    baska bir bolumu verirse StreamError'dur - tam dosya yazmak akisi
    kopyalarla sisirirdi. Eksik govde (kesilen okuma) yeniden denenebilir
    oldugu icin ConnectionError'dur.
    """
    end = start + length - 1
    opener = httpcli._make_opener(opt)
    req = httpcli.request_of(url, opt)
    req.add_header("Range", f"bytes={start}-{end}")
    with opener.open(req, timeout=opt.timeout) as r:
        status = getattr(r, "status", 0)
        cr = (r.headers.get("Content-Range") or "").strip()
        m = _CR_RE.match(cr)
        if status != 206 or m is None or int(m.group(1)) != start:
            raise StreamError(
                f"segment {start}-{end} icin gecersiz Range yaniti "
                f"(HTTP {status}, Content-Range {cr or '-'})")
        data = _read_body(r, bucket, stop)
    if data is None:
        return None
    if len(data) != length:
        raise ConnectionError(
            f"segment {start}-{end}: eksik govde ({len(data)}/{length})")
    return data


_TERMINAL_HTTP = frozenset({
    400, 401, 403, 404, 405, 410, 411, 412, 413, 414, 415, 416, 422, 451})


def is_transient(e: BaseException) -> bool:
    """Bu hata yeniden denenmeli mi? (404/403/auth gibi kalici hatalar degil).

    429 ve 5xx, zaman asimlari, baglanti sifirlanmalari ve eksik govde
    gecicidir; Range dogrulama hatalari (StreamError) ve 4xx kalicidir.
    """
    if isinstance(e, StreamError):
        return False
    if isinstance(e, urllib.error.HTTPError):
        if e.code in _TERMINAL_HTTP:
            return False
        return e.code in (408, 425, 429) or 500 <= e.code <= 599
    if isinstance(e, urllib.error.URLError):
        r = e.reason
        if isinstance(r, ssl.SSLError):
            return False
        return isinstance(r, (TimeoutError, ConnectionError, OSError)) \
            or isinstance(r, str)
    return isinstance(e, (TimeoutError, ConnectionError,
                          http.client.HTTPException, OSError))


def retry_delay(attempt: int, err: BaseException) -> float:
    """Segment yeniden denemesi icin bekleme: 429/503'te Retry-After'a
    uyulur, digerlerinde ustel geri cekilme (`job.backoff_delay`)."""
    from .job import _retry_after, backoff_delay   # dongusel: cagri aninda
    if isinstance(err, urllib.error.HTTPError) and err.code in (429, 503):
        return _retry_after(err)
    return backoff_delay(attempt)


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
             seg_urls: list, *, store=None, cap: int = SEG_MAX,
             rtt: float = 0.0) -> None:
    """init_url + seg_urls sirali birlesimi. meta icinde: done_segments,
    cursor, init_done. progress_cb(byte) cekim sirasinda cagrilir.

    `seg_urls` ogesi duz URL ya da `Segment` olabilir (alt aralik/sifre
    cozme). Gecici hatalar segment basina `opt.retries` kez geri cekilmeyle
    yeniden denenir; kalici hatalar (404/403/auth) isi hemen bitirir.

    store verilirse (HostProfiles) havuz isci sayisi ogrenilir: tohum
    profilden okunur, kosu sonunda ya da duraklamada geri bildirilir.
    rtt: bu kosudaki manifest yanit suresi (RTT bandi icin)."""
    done: set[int] = set(int(i) for i in meta.get("done_segments", []))
    meta["segments_total"] = len(seg_urls)
    segs_dir = temp_dir(meta_path)
    segs_dir.mkdir(parents=True, exist_ok=True)

    entries = [_as_segment(u) for u in seg_urls]
    host_url = entries[0].url if entries else ""
    seed = 0
    if store is not None and host_url:
        try:
            seed = int((store.get(host_url) or {}).get("seg_workers") or 0)
        except Exception:
            seed = 0            # profil okunamadi: soguk baslangic
    workers = pool_size(len(entries), seed, cap)

    errors: list[str] = []
    failure: list[BaseException] = []   # kod tasiyan asil hata (StreamError)
    lock = threading.Lock()
    last_save = [0.0]
    fetched = [0]              # bu kosuda agdan inen segment sayisi
    throttled = [False]
    retries = max(0, int(getattr(opt, "retries", 5)))

    def persist(force=False):
        now = time.monotonic()
        if not force and now - last_save[0] < 2.0:
            return
        last_save[0] = now
        # atomik + dayanikli: yarim/sifir dolu nokta dosyasi kalmasin
        util.atomic_write_json(meta_path, meta)

    def save_cursor(cur: int, tail: bool = False):
        """Cikti fd'sini fsync eder, SONRA ilerlemeyi meta'ya yazar.

        Kayit ancak baytlar diske indikten sonra ilerler: guc kesintisi
        'tamamlanan segment' diye sifir dolu bolum birakmaz. Aralikli
        yazimda 2 sn'de bir, bitiste kosulsuz."""
        now = time.monotonic()
        if not tail and now - last_save[0] < 2.0:
            return
        os.fsync(fd)
        meta["done_segments"] = sorted(done)
        meta["cursor"] = cur
        if tail:
            meta["tail"] = cur
        persist(force=True)

    def seg_file(idx: int) -> Path:
        return segs_dir / f"{idx:06d}.seg"

    def work(idx: int):
        if stop_event.is_set() or errors:
            return
        f = seg_file(idx)
        if not f.exists():
            seg = entries[idx]
            attempt = 0
            while True:
                try:
                    if seg.start is not None:
                        data = fetch_range(seg.url, opt, bucket, seg.start,
                                           seg.length, stop_event)
                    else:
                        data = fetch_all(seg.url, opt, bucket, stop_event)
                    if data is not None and seg.key is not None:
                        data = _decrypt_aes128(data, seg.key, seg.iv)
                    break
                except Exception as e:
                    if isinstance(e, urllib.error.HTTPError):
                        e.close()      # kaynak sizmasin (yanit okunmadi)
                    # 429/503: sunucu eszamanli baglantiyi kisitliyor olabilir
                    if isinstance(e, urllib.error.HTTPError) \
                            and e.code in (429, 503):
                        with lock:
                            throttled[0] = True
                    if stop_event.is_set() or not is_transient(e) \
                            or attempt >= retries:
                        with lock:
                            errors.append(f"segment {idx}: {e}")
                            failure.append(e)
                        return
                    delay = retry_delay(attempt, e)
                    attempt += 1
                    # stop-aware bekleme: duraklat/iptal beklemeden doner
                    if stop_event.wait(delay):
                        return
            if data is None:          # duraklat/iptal: yarim parca yazilmaz
                return
            if stop_event.is_set():
                return
            tmp = f.with_suffix(".part")
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())     # .seg adi ancak icerik diske inince
            tmp.replace(f)
            with lock:
                fetched[0] += 1
        with lock:
            if progress_cb and idx not in done:
                progress_cb(f.stat().st_size)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, range(len(entries))))
    _learn(store, host_url, workers, fetched[0], throttled[0], rtt)
    if stop_event.is_set():
        persist(force=True)
        return
    if errors:
        # kod tasiyan hata (sifre cozme/Range) korunur: arayuz dogru kodu
        # gostersin; digerleri eski davranisla RuntimeError'a sarilir
        if failure and isinstance(failure[0], StreamError):
            raise failure[0]
        raise RuntimeError(errors[0])

    cursor = int(meta.get("cursor", 0))
    if init_url is not None and not meta.get("init_done"):
        data = fetch_all(init_url, opt, bucket, stop_event)
        if data is None:              # duraklat/iptal
            persist(force=True)
            return
        os.pwrite(fd, data, 0)
        cursor = len(data)
        os.fsync(fd)                  # meta 'init_done' derken veri diskte
        meta["init_done"] = True
        meta["cursor"] = cursor
    for idx in range(len(entries)):
        if stop_event.is_set():
            persist(force=True)
            return
        if idx in done:
            continue
        data = seg_file(idx).read_bytes()
        os.pwrite(fd, data, cursor)
        cursor += len(data)
        done.add(idx)
        save_cursor(cursor)
    os.ftruncate(fd, cursor)
    save_cursor(cursor, tail=True)
    if progress_cb:
        progress_cb(0)
    cleanup(meta_path)
