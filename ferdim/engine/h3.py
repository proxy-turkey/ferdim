"""HTTP/3 (QUIC) tasimasi - istege bagli aioquic eklentisi.

Cekirdek stdlib'de kalir: aioquic yalnizca bu modul icinde ve yalnizca
gercekten HTTP/3 kullanilacaksa tembel yuklenir (modul importu aioquic
gerektirmez).

Modlar (DlOptions.http3):
  off   - h3 hic denenmez (varsayilan)
  auto  - h3 yalnizca kaynak daha once TCP yanitinda
          `Alt-Svc: h3=":port"` duyurduysa ve o kaynak "bozuk"
          isaretli degilse denenir; herhangi bir h3 hatasinda istek
          TCP'ye duser.
  force - h3 ya da hata (proxy/http semasi/aioquic yok ise hata).

Bir h3 denemesi baglanti/akis duzeyinde basarisiz olursa kaynak 10
dakika "bozuk" isaretlenir: auto modda sonraki araliklar dogrudan TCP
kullanir (aktarim ortasinda dusen araligi job.py yeniden ister).

Baglanti havuzu: butun h3 baglantilari TEK bir arka plan is parcaciginda
kosan tek bir olay dongusunu paylasir. Hedef basina (host/port, SNI,
dogrulama modu) bir QUIC baglantisi tutulur ve es zamanli istekler o
baglanti uzerinde AYRI AKISLAR olarak gider (HTTP/3 coklama): her aralik
yeni bir el sikismasi yapmaz. Baglanti basina es zamanli akis siniri
MAX_STREAMS; asilirsa ayni hedef icin ikinci baglanti acilir. Bos
baglanti POOL_IDLE_SECS sonra kapanir. Baglanti hatasi havuz kaydini
dusurur ve bekleyen akislari H3Error ile uyandirir (auto modda TCP'ye
dusulur).

Veri, olay dongusu tarafindan akis basina bir kuyruga birakilir; bloklayan
Response okumasi oradan beslenir ve akis bitince kuyrukta kalan son
parcalar kaybolmaz (okur sentinel'i gorene kadar orada durur). Her okuma
stall zaman asimina tabidir.

Geri basinc: aioquic akis kredisini alinan bayta gore otomatik artirdigi
icin akis duzeyinde kredi freni yoktur. Bu yuzden tampon akis basina
STREAM_BUFFER ile sinirlanir: sinir asilinca baglantisinin UDP okumasi
duraklatilir (cekirdek kuyrugu ve QUIC akis kredisi peer'i durdurur), okur
tamponu yariya indirince okuma surdurulur. Ayni baglantidaki akislar bu
duraklamayi paylasir; veri kaybolmaz, QUIC yeniden gonderir.

Uygulama kapanirken h3.close_all() cagrilir: olay dongusu ve UDP soketleri
boylece serbest kalir.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import email.message
import ssl
import threading
import time
import urllib.error
from urllib.parse import SplitResult, urlsplit

from ..profiles import normalise_host

MODES = ("off", "auto", "force")
BROKEN_SECS = 600.0             # h3 hatasi sonrasi TCP'de kalma suresi
ALT_SVC_DEFAULT_MAX_AGE = 86400.0
JOIN_SECS = 3.0
CONNECT_SECS = 5.0              # el sikismasi ust siniri (stall'dan bagimsiz)
POOL_IDLE_SECS = 30.0           # bos baglanti bu sure sonra kapanir
POOL_SWEEP_SECS = 5.0           # bos baglanti tarama araligi
MAX_STREAMS = 32                # baglanti basina es zamanli akis (= max link)
# akis basina okunmayi bekleyen tampon siniri: asilinca baglantisinin UDP
# okumasi duraklatilir (geri basinc), okur tamponu yariya indirince acilir
STREAM_BUFFER = 1 << 20
CLOSE_SECS = 5.0                # baglanti kapatma ust siniri
LOOP_START_SECS = 5.0

# h3'te baglantiya ozgu (yasak) basliklar gonderilemez
_HOP = frozenset({"connection", "keep-alive", "proxy-connection", "host",
                  "transfer-encoding", "upgrade", "te"})
_REDIRECT = frozenset({301, 302, 303, 307, 308})

_API = None                     # aioquic sembolleri (tembel)
_AVAILABLE: bool | None = None  # available() sonucu (negatif dahil) onbellek
_PROTO = None                   # QuicConnectionProtocol alt sinifi (tembel)
# Onbellek anahtari: origin + ("|insecure" guvenilmez TLS isleri icin).
# Guvenilmez isin ogrendigi kayit guvenli ise sizmaz (ve tersi).
# origin -> (host, port, bitis); host "" = kaynagin kendi adresi
_ALT_SVC: dict[str, tuple[str, int, float]] = {}
_BROKEN: dict[str, float] = {}                # origin -> bitis zamani
# Onbellekler kaynak sayisiyla sinirsiz buyumez (LRU, en eski atilir).
MAX_CACHE = 256

_POOL: dict[tuple, list[_Conn]] = {}          # hedef -> canli baglantilar
_OPENING: dict[tuple, asyncio.Future] = {}    # hedef -> kurulmakta olan
_LOCK = threading.Lock()
_LOOPS: "_Loop | None" = None                 # paylasilan olay dongusu
_PEAK_BUFFERED = 0              # testler icin: okunmamis tampon zirvesi


class H3Unavailable(RuntimeError):
    """h3 bu kaynak/ayar icin kullanilamaz (force modda hata)."""


class H3Error(RuntimeError):
    """h3 baglantisi kurulamadi ya da akis koptu."""


def _now() -> float:
    return time.monotonic()


# ------------------------------------------------------------- aioquic
def _load():
    """aioquic sembollerini tembel yukler; yoksa ImportError."""
    global _API
    if _API is not None:
        return _API
    with _LOCK:
        if _API is not None:
            return _API
        from aioquic.asyncio import QuicConnectionProtocol, connect
        from aioquic.h3.connection import H3_ALPN, H3Connection
        from aioquic.h3.events import DataReceived, HeadersReceived
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.events import ConnectionTerminated, StreamReset
        _API = {
            "connect": connect,
            "QuicConnectionProtocol": QuicConnectionProtocol,
            "H3Connection": H3Connection,
            "H3_ALPN": H3_ALPN,
            "DataReceived": DataReceived,
            "HeadersReceived": HeadersReceived,
            "ConnectionTerminated": ConnectionTerminated,
            "StreamReset": StreamReset,
            "QuicConfiguration": QuicConfiguration,
        }
    return _API


def available() -> bool:
    """aioquic kurulu mu? Sonuc (yoksa da) onbelleklenir."""
    global _AVAILABLE
    if _AVAILABLE is None:
        try:
            _load()
        except Exception:
            _AVAILABLE = False    # her cagrida yeniden import denenmesin
        else:
            _AVAILABLE = True
    return _AVAILABLE


# ------------------------------------------------------- Alt-Svc onbellegi
def origin(url: str) -> str:
    """Sema + ana makine + port (Alt-Svc/bozuk kaynak anahtari).

    Ana makine IDNA'ya normalise edilir: ayni sunucunun unicode ve punycode
    yazimlari tek onbellek kaydini paylasir."""
    p = urlsplit(url)
    port = p.port or (443 if p.scheme == "https" else 80)
    return f"{p.scheme}://{normalise_host(p.hostname or '')}:{port}"


def parse_alt_svc(value: str | None) -> tuple[str, int, float] | None:
    """`Alt-Svc` basligindan (host, port, max-age) cikarir; h3 yoksa None.

    host bos ise hizmet kaynagin kendi ana makinesindedir; dolu ise
    baglanti o ana makineye kurulur (yetki/SNI yine kaynagin adidir).
    Birden fazla h3 girdisinde en uzun omurlu secilir.
    """
    best = None
    for item in (value or "").split(","):
        fields = [f.strip() for f in item.split(";")]
        if not fields or not fields[0]:
            continue
        proto, sep, target = fields[0].partition("=")
        if not sep or proto.strip().lower() != "h3":
            continue
        authority = target.strip().strip('"')
        max_age = ALT_SVC_DEFAULT_MAX_AGE
        for field in fields[1:]:
            key, _, val = field.partition("=")
            if key.strip().lower() == "ma":
                try:
                    max_age = float(val.strip().strip('"'))
                except ValueError:
                    pass
        if ":" not in authority:
            continue
        alt_host, _, port = authority.rpartition(":")
        if not port.isdigit():
            continue
        alt_host = alt_host.strip("[]").lower()
        if best is None or max_age > best[2]:
            best = (alt_host, int(port), max_age)
    return best


def _cache_key(url: str, insecure: bool = False) -> str:
    """Alt-Svc anahtari: origin (+ guvenilmez TLS isaretcisi)."""
    return origin(url) + ("|insecure" if insecure else "")


def _insecure(opt) -> bool:
    return bool(getattr(opt, "insecure", False))


def _evict(cache: dict, now: float, deadline) -> None:
    """Onbellegi MAX_CACHE'te tutar: once suresi dolanlar, sonra en eski."""
    if len(cache) <= MAX_CACHE:
        return
    for key in [k for k, v in cache.items() if deadline(v) <= now]:
        cache.pop(key, None)
    while len(cache) > MAX_CACHE:
        cache.pop(next(iter(cache)), None)


def remember_alt_svc(url: str, value: str | None,
                     insecure: bool = False) -> bool:
    """TCP yanitindaki Alt-Svc basligini onbellege yazar (ma=0 siler).

    Yalniz yanitin SON adresinin origin'i icin cagrilmalidir: yonlendirme
    hedefi baska bir kaynagin h3 ayarini belirleyemez. Guvenilmez (TLS
    dogrulamasiz) islerin ogrendigi kayit ayri tutulur, guvenli ise
    sizmaz."""
    parsed = parse_alt_svc(value)
    if parsed is None:
        return False
    alt_host, port, max_age = parsed
    key = _cache_key(url, insecure)
    now = _now()
    with _LOCK:
        if max_age <= 0:
            _ALT_SVC.pop(key, None)
            return False
        _ALT_SVC.pop(key, None)          # LRU: guncellenen sona gider
        _ALT_SVC[key] = (alt_host, port, now + max_age)
        _evict(_ALT_SVC, now, lambda v: v[2])
    return True


def alt_svc_target(url: str, insecure: bool = False) -> tuple[str, int] | None:
    """Onbellekteki h3 hedefi (host, port); yoksa/suresi dolduysa None.

    host bos ise kaynagin kendi ana makinesine baglanilir."""
    key = _cache_key(url, insecure)
    now = _now()
    with _LOCK:
        item = _ALT_SVC.get(key)
        if item is None:
            return None
        if item[2] <= now:
            _ALT_SVC.pop(key, None)
            return None
        _ALT_SVC.pop(key, None)          # LRU dokunusu
        _ALT_SVC[key] = item
        return item[0], item[1]


def mark_broken(url: str) -> None:
    """h3 denemesi basarisiz: kaynak BROKEN_SECS boyunca TCP'de kalir."""
    key = origin(url)
    now = _now()
    with _LOCK:
        _BROKEN.pop(key, None)
        _BROKEN[key] = now + BROKEN_SECS
        _evict(_BROKEN, now, lambda v: v)


def broken(url: str) -> bool:
    key = origin(url)
    with _LOCK:
        until = _BROKEN.get(key)
        if until is None:
            return False
        if until <= _now():
            _BROKEN.pop(key, None)
            return False
        return True


def reset_cache() -> None:
    """Testler icin: Alt-Svc/bozuk kaynak onbellegi ve tampon zirvesi."""
    global _PEAK_BUFFERED
    with _LOCK:
        _ALT_SVC.clear()
        _BROKEN.clear()
    _PEAK_BUFFERED = 0


# --------------------------------------------------------- mod secimi
def mode_of(opt) -> str:
    """DlOptions.http3 -> "off" | "auto" | "force" (gecersiz = off)."""
    mode = str(getattr(opt, "http3", "off") or "off").strip().lower()
    return mode if mode in MODES else "off"


def _why_not(url: str, opt) -> str:
    if urlsplit(url).scheme != "https":
        return "h3 yalnizca https icin"
    if getattr(opt, "proxy", ""):
        return "proxy varsa h3 kullanilmaz"
    if not available():
        return "aioquic kurulu degil"
    return ""


def use_h3(url: str, opt) -> bool:
    """Bu istek h3 uzerinden mi gitmeli? force modda kosul yoksa hata."""
    mode = mode_of(opt)
    if mode == "off":
        return False
    reason = _why_not(url, opt)
    if reason:
        if mode == "force":
            raise H3Unavailable(reason)
        return False
    if mode == "force":
        return True
    return not broken(url) and alt_svc_target(url, _insecure(opt)) is not None


# --------------------------------------------------------- istek/yanit
def _authority(parts: SplitResult) -> str:
    host = normalise_host(parts.hostname or "")
    if ":" in host:
        host = f"[{host}]"
    port = parts.port or 443
    return host if port == 443 else f"{host}:{port}"


def _request_headers(parts: SplitResult,
                     headers: dict | None) -> list[tuple[bytes, bytes]]:
    """HTTP/3 baslik listesi: pseudo basliklar + baglantiya ozgu olmayanlar."""
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    out = [
        (b":method", b"GET"),
        (b":scheme", b"https"),
        (b":authority", _authority(parts).encode("latin-1")),
        (b":path", path.encode("utf-8", "replace")),
    ]
    for key, val in (headers or {}).items():
        name = str(key).strip().lower()
        if not name or name.startswith(":") or name in _HOP:
            continue
        out.append((name.encode("latin-1"),
                    str(val).encode("latin-1", "replace")))
    return out


def _message(headers) -> email.message.Message:
    """h3 basliklarini email.message'a cevirir (get/get_filename uyumlu)."""
    msg = email.message.Message()
    for key, val in headers:
        if key.startswith(b":"):
            continue
        msg[key.decode("latin-1")] = val.decode("latin-1")
    return msg


def _status_of(headers) -> int:
    for key, val in headers:
        if key == b":status":
            try:
                return int(val)
            except ValueError:
                return 0
    return 0


# ----------------------------------------------------- paylasilan dongu
class _Loop:
    """Butun h3 baglantilarinin paylastigi tek olay dongusu/is parcacigi."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.sweeper: asyncio.Task | None = None
        self.thread = threading.Thread(target=self._run, name="ferdim-h3-loop",
                                       daemon=True)

    def start(self) -> asyncio.AbstractEventLoop:
        self.thread.start()
        self.ready.wait(LOOP_START_SECS)
        return self.loop

    def _run(self) -> None:
        loop = self.loop
        asyncio.set_event_loop(loop)
        self.sweeper = loop.create_task(_sweeper())
        loop.call_soon(self.ready.set)
        try:
            loop.run_forever()
        finally:
            _drain_tasks(loop)
            loop.close()


def _drain_tasks(loop: asyncio.AbstractEventLoop) -> None:
    """Dongu kapanmadan bekleyen gorevleri iptal edip toplar."""
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:
        pass


def _loop() -> asyncio.AbstractEventLoop:
    """Paylasilan h3 olay dongusu (ilk istekte tembel baslar)."""
    global _LOOPS
    with _LOCK:
        if _LOOPS is None or _LOOPS.loop.is_closed():
            _LOOPS = _Loop()
            _LOOPS.start()
        return _LOOPS.loop


def close_all() -> None:
    """Butun h3 baglantilarini ve paylasilan olay dongusunu kapatir.

    Uygulama kapanisinda cagrilir (daemon/CLI): is parcacigi, UDP
    soketleri ve bekleyen akislar serbest kalir. Sonraki istek donguyu
    yeniden kurar."""
    global _LOOPS
    with _LOCK:
        state, _LOOPS = _LOOPS, None
    if state is None:
        return
    loop = state.loop
    if not loop.is_closed():
        try:
            fut = asyncio.run_coroutine_threadsafe(_shutdown(state), loop)
            fut.result(CLOSE_SECS + JOIN_SECS)
        except Exception:
            pass
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
    if state.thread is not threading.current_thread():
        state.thread.join(JOIN_SECS)


async def _shutdown(state: "_Loop") -> None:
    """Baglantilari kapatir, supurmeyi durdurur (olay dongusunde)."""
    conns = _pool_all()
    for conn in conns:
        _conn_dead(conn, H3Error("h3: kapatildi"), broken=False)
    for conn in conns:
        await _conn_close(conn)
    _OPENING.clear()
    sweeper = state.sweeper
    if sweeper is not None and not sweeper.done():
        sweeper.cancel()
        try:
            await sweeper
        except BaseException:
            pass


async def _sweeper() -> None:
    """Bos baglantilari POOL_IDLE_SECS sonra kapatir."""
    while True:
        await asyncio.sleep(POOL_SWEEP_SECS)
        now = _now()
        for conn in _pool_all():
            if conn.closed or conn.streams:
                continue
            if now - conn.last_used < POOL_IDLE_SECS:
                continue
            await _conn_close(conn)


# ------------------------------------------------------------- havuz
class _Conn:
    """Bir hedefe kurulmus kalici QUIC baglantisi (havuz kaydi)."""

    def __init__(self, key: tuple, url: str) -> None:
        self.key = key
        self.url = url            # ilk istek: bozulma isareti bu origin'e
        self.proto = None
        self.acm = None           # connect() baglam yoneticisi (kapanis icin)
        self.streams: dict[int, _Stream] = {}
        self.last_used = _now()
        self.closed = False
        self.paused = False       # UDP okumasi durdu (okur geri basinci)

    def pause(self) -> None:
        """Okumayi duraklat: bir akisin tamponu STREAM_BUFFER'i asti."""
        if self.paused:
            return
        self.paused = True
        self._reading(False)

    def resume(self) -> None:
        """Okumayi surdur: tamponlar eridi (yari sinirin altinda)."""
        if not self.paused:
            return
        self.paused = False
        self._reading(True)

    def _reading(self, on: bool) -> None:
        proto = self.proto
        transport = getattr(proto, "_transport", None) if proto else None
        if transport is None:
            return
        try:
            if on:
                transport.resume_reading()
            else:
                transport.pause_reading()
        except Exception:          # tasima kapanmis olabilir
            pass


class _Stream:
    """Tek bir HTTP/3 istegi (akis): yanit basliklari + veri kuyrugu."""

    def __init__(self) -> None:
        self.conn: _Conn | None = None
        self.stream_id = -1
        self.queue: asyncio.Queue | None = None
        self.ready: asyncio.Future | None = None
        self.status = 0
        self.headers: email.message.Message | None = None
        self.got_headers = False
        self.server_done = False      # sunucu akisi bitirdi (FIN geldi)
        self.reader_done = False      # okur bitirdi: baglanti yeniden kullanilir
        self.failed = False
        self.error: BaseException | None = None
        self.buffered = 0             # kuyrukta okunmayi bekleyen bayt
        self._buf_lock = threading.Lock()

    def on_headers(self, headers) -> None:
        if self.got_headers:          # izleyen HEADERS = trailer
            return
        self.got_headers = True
        self.status = _status_of(headers)
        self.headers = _message(headers)
        self._wake()

    def feed(self, data: bytes) -> None:
        if self.queue is None:
            return
        self.queue.put_nowait(data)
        with self._buf_lock:          # okur baska is parcaciginda sayar
            self.buffered += len(data)

    def take_buffered(self, n: int) -> None:
        with self._buf_lock:
            self.buffered -= n

    def end(self) -> None:
        if self.server_done:
            return
        self.server_done = True
        if self.queue is not None:
            self.queue.put_nowait(None)
        self._wake()

    def fail(self, ex: BaseException) -> None:
        if self.failed:
            return
        self.failed = True
        self.server_done = True
        self.error = ex
        if self.queue is not None:
            self.queue.put_nowait(ex)
        self._wake()

    def _wake(self) -> None:
        if self.ready is not None and not self.ready.done():
            self.ready.set_result(None)


def _pool_all() -> list[_Conn]:
    with _LOCK:
        return [c for items in list(_POOL.values()) for c in items]


def _pool_pick(key: tuple) -> _Conn | None:
    """Ayni hedefin kapasiteli baglantisi (en az akisli olan)."""
    with _LOCK:
        items = [c for c in _POOL.get(key, ()) if not c.closed]
    live = [c for c in items
            if c.acm is not None and len(c.streams) < MAX_STREAMS]
    if not live:
        return None
    return min(live, key=lambda c: len(c.streams))


def _pool_remove(conn: _Conn) -> None:
    with _LOCK:
        items = _POOL.get(conn.key)
        if not items:
            return
        if conn in items:
            items.remove(conn)
        if not items:
            _POOL.pop(conn.key, None)


def pool_stats() -> dict:
    """Testler icin: canli baglanti/akis ve tampon sayilari."""
    conns = _pool_all()
    streams = [s for c in conns for s in c.streams.copy().values()]
    return {"connections": len(conns),
            "streams": len(streams),
            "buffered": sum(s.buffered for s in streams),
            "peak_buffered": _PEAK_BUFFERED}


def _note_buffered(conn: _Conn) -> None:
    """Testler icin: baglanti basina okunmamis bayt zirvesini isler."""
    global _PEAK_BUFFERED
    total = sum(s.buffered for s in conn.streams.values())
    if total > _PEAK_BUFFERED:
        _PEAK_BUFFERED = total


def _resume_if_dry(conn: _Conn) -> None:
    """Butun akislarin tamponu yariya indiyse okumayi surdurur (dongude)."""
    if not conn.paused or conn.closed:
        return
    if any(s.buffered >= STREAM_BUFFER // 2 for s in conn.streams.values()):
        return
    conn.resume()


def _key_of(url: str, opt, target: tuple[str, int] | None) -> tuple:
    """Havuz anahtari: (hedef host, port, SNI, dogrulama modu)."""
    parts = urlsplit(url)
    host = normalise_host(parts.hostname or "")
    port = parts.port or 443
    if target is not None:
        host = normalise_host(target[0] or host) or host
        port = target[1]
    sni = normalise_host(parts.hostname or "") or host
    return (host, port, sni, bool(getattr(opt, "insecure", False)))


def _release_stream(stream: _Stream, reset: bool = False) -> None:
    """Akis kaydini kapatir (olay dongusunde).

    reset: okur yarida birakti -> sunucuya STOP_SENDING gonderilir. Akis
    sunucu tarafinda bittiyse yalnizca kayit dusurulur."""
    if stream.reader_done:
        return
    stream.reader_done = True
    conn = stream.conn
    if conn is None:
        return
    if conn.streams.get(stream.stream_id) is stream:
        conn.streams.pop(stream.stream_id, None)
    conn.last_used = _now()
    proto = conn.proto
    if (reset and not conn.closed and not stream.server_done
            and stream.stream_id >= 0 and proto is not None):
        try:
            proto._quic.stop_stream(stream.stream_id, 0)
            proto.transmit()
        except Exception:
            pass
    _resume_if_dry(conn)          # duraklatan akis gitti: okuma acilabilir


def _conn_dead(conn: _Conn, err: BaseException, broken: bool = True) -> None:
    """Baglanti oldu: havuzdan cikar, bekleyen akislari hatayla uyandir.

    broken: kaynak "bozuk" isaretlenir (auto modda TCP'ye dusulur). Temiz
    bosta kapanmada ya da uygulama kapanisinda bu yapilmaz."""
    if conn.closed:
        return
    conn.closed = True
    _pool_remove(conn)
    streams = list(conn.streams.values())
    conn.streams.clear()
    for stream in streams:
        stream.fail(err)
    if broken and streams:       # veri kaybeden akis varsa kaynak bozuk
        mark_broken(conn.url)
    try:
        asyncio.get_running_loop().create_task(_conn_close(conn))
    except RuntimeError:
        pass


def _kill_transport(conn: _Conn) -> None:
    proto = conn.proto
    transport = getattr(proto, "_transport", None) if proto is not None else None
    if transport is not None:
        try:
            transport.close()
        except Exception:
            pass


async def _conn_close(conn: _Conn) -> None:
    """Baglantiyi kapatir (idempotent) ve havuzdan cikarir."""
    acm, conn.acm = conn.acm, None
    conn.closed = True
    _pool_remove(conn)
    if acm is None:
        return
    try:
        await asyncio.wait_for(acm.__aexit__(None, None, None), CLOSE_SECS)
    except Exception:
        _kill_transport(conn)


def _quic_config(sni: str, insecure: bool, tls=None):
    """QUIC istemci yapilandirmasi; `tls` verilirse CA/mTLS uygulanir.

    QUIC zaten yalniz TLS 1.3 kullanir; asgari surum ayri bir anahtar
    gerektirmez. Dogrulama ayari SNI ile kaynagin adina baglidir."""
    api = _load()
    conf = api["QuicConfiguration"](
        is_client=True,
        alpn_protocols=list(api["H3_ALPN"]),
        # SNI/ana makine dogrulamasi her zaman KAYNAGIN adiyla
        server_name=sni,
        verify_mode=ssl.CERT_NONE if insecure else ssl.CERT_REQUIRED,
    )
    if tls is not None:
        tls.apply_quic(conf)
    return conf


async def _conn_open(key: tuple, host: str, port: int, sni: str,
                     insecure: bool, url: str, opt) -> _Conn:
    """Yeni bir QUIC baglantisi kurar (el sikismasi dahil)."""
    api = _load()
    conf = _quic_config(sni, insecure, getattr(opt, "tls", None))
    conn = _Conn(key, url)
    proto_cls = _protocol_class()

    def make(quic, stream_handler=None):
        proto = proto_cls(quic, stream_handler=stream_handler)
        conn.proto = proto
        proto.conn = conn          # el sikismasi olaylari da baglaniyor
        return proto

    acm = api["connect"](host, port, configuration=conf, create_protocol=make)
    try:
        await acm.__aenter__()
    except BaseException as ex:
        conn.closed = True
        if isinstance(ex, asyncio.CancelledError):
            raise
        raise H3Error(f"h3: baglanti kurulamadi: {ex}") from ex
    conn.acm = acm
    if conn.closed:               # el sikismasi biterken baglanti dustu
        await _conn_close(conn)
        raise H3Error("h3: baglanti kurulamadi")
    with _LOCK:
        _POOL.setdefault(key, []).append(conn)
    return conn


async def _conn_get(url: str, opt, target: tuple[str, int] | None) -> _Conn:
    """Havuzdan kapasiteli baglanti; yoksa yenisini kurar.

    Ayni hedef icin es zamanli istekler tek el sikismasi paylasir
    (_OPENING); MAX_STREAMS asilirsa yeni baglanti acilir."""
    key = _key_of(url, opt, target)
    host, port, sni, insecure = key
    loop = asyncio.get_running_loop()
    for _ in range(8):
        conn = _pool_pick(key)
        if conn is not None:
            return conn
        pending = _OPENING.get(key)
        if pending is None:
            pending = loop.create_future()
            _OPENING[key] = pending
            try:
                conn = await _conn_open(key, host, port, sni, insecure,
                                        url, opt)
            except BaseException:
                if not pending.done():
                    pending.set_result(None)
                raise
            else:
                if not pending.done():
                    pending.set_result(conn)
                return conn
            finally:
                _OPENING.pop(key, None)
        conn = await asyncio.shield(pending)
        if conn is None or conn.closed:
            continue
        if len(conn.streams) < MAX_STREAMS:
            return conn
        # baglanti doldu: dongu ayni hedef icin yeni baglanti acar
    raise H3Error("h3: baglanti kurulamadi")


def _protocol_class():
    """H3Connection'i besleyen protokol sinifi (bir kez uretilir)."""
    global _PROTO
    if _PROTO is not None:
        return _PROTO
    api = _load()

    class Proto(api["QuicConnectionProtocol"]):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.h3 = api["H3Connection"](self._quic)
            self.conn: _Conn | None = None

        def quic_event_received(self, event):
            conn = self.conn
            if conn is None:
                return
            if isinstance(event, api["ConnectionTerminated"]):
                _conn_dead(conn, H3Error(
                    f"h3: baglanti kapandi (kod {event.error_code})"))
                return
            if isinstance(event, api["StreamReset"]):
                stream = conn.streams.get(event.stream_id)
                if stream is not None:
                    stream.fail(H3Error("h3: akis sifirlandi"))
                return
            try:
                events = self.h3.handle_event(event)
            except Exception as ex:       # protokol bozulmasi: baglanti oldu
                _conn_dead(conn, H3Error(f"h3: akis hatasi: {ex}"))
                return
            for ev in events:
                stream = conn.streams.get(ev.stream_id)
                if stream is None:
                    continue              # bitmis/terk edilmis akis
                data = getattr(ev, "data", None)
                if isinstance(ev, api["HeadersReceived"]):
                    stream.on_headers(ev.headers)
                elif data:
                    stream.feed(data)
                    _note_buffered(conn)
                    if stream.buffered >= STREAM_BUFFER:
                        conn.pause()      # okur yetismiyor: okumayi durdur
                if getattr(ev, "stream_ended", False):
                    stream.end()

    _PROTO = Proto
    return Proto


class _Session:
    """Tek bir h3 istegi: akis kaydi + bloklayan okuma koprusu.

    target: (host, port) - Alt-Svc duyurusu; None ise URL'in kendi
    adresi. Baglanti bu hedefe kurulur, istek yine kaynak adina gider.
    """

    def __init__(self, url: str, opt, headers: dict | None,
                 timeout: float, target: tuple[str, int] | None = None):
        self.url = url
        self.opt = opt
        self.headers = headers or {}
        self._timeout = float(timeout or 30)
        # el sikismasi, stall suresinden bagimsiz olarak sinirlidir: auto
        # modda basarisiz h3 denemesi TCP'ye hizli dussun
        self._connect_timeout = min(self._timeout, CONNECT_SECS)
        self._target = target
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream: _Stream | None = None
        self._queue: asyncio.Queue | None = None
        self._status = 0
        self._headers: email.message.Message | None = None
        self._error: BaseException | None = None
        self._buf = bytearray()
        self._eof = False
        self._closed = False

    # --- dis arayuz ---------------------------------------------------
    def start(self) -> tuple[int, email.message.Message]:
        """Akisi acar ve yanit basliklarini bekler (zaman asimli)."""
        self._loop = _loop()
        stream = _Stream()
        self._stream = stream
        fut = asyncio.run_coroutine_threadsafe(self._open(), self._loop)
        try:
            fut.result(self._connect_timeout)
        except concurrent.futures.TimeoutError:
            # 3.9/3.10'da concurrent.futures.TimeoutError ayri bir siniftir
            # (builtin TimeoutError degil): yakalanmazsa istek iptal
            # edilmez, sonradan akis kaydeder ve slot sizdirirdi
            fut.cancel()
            self._release(reset=True)
            mark_broken(self.url)
            raise TimeoutError("h3: yanit basligi gelmedi") from None
        except concurrent.futures.CancelledError:
            self._release(reset=True)
            raise H3Error("h3: istek iptal edildi") from None
        except BaseException:
            err = self._error
            self._release(reset=True)
            if err is not None:
                raise err
            raise H3Error("h3: istek basarisiz") from None
        return self._status, self._headers or email.message.Message()

    def read(self, n: int = -1) -> bytes:
        if self._error is not None:
            raise self._error
        if n is None or n < 0:
            while not self._eof:
                self._fill()
            out = bytes(self._buf)
            self._buf.clear()
            return out
        while len(self._buf) < n and not self._eof:
            self._fill()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._release(reset=True)

    # --- okuma koprusu ------------------------------------------------
    def _fill(self) -> None:
        if self._error is not None:
            raise self._error
        if self._eof or self._closed:
            self._eof = True
            return
        loop, queue = self._loop, self._queue
        if loop is None or queue is None or loop.is_closed():
            raise H3Error("h3: baglanti hazir degil")
        try:
            waiter = asyncio.run_coroutine_threadsafe(queue.get(), loop)
        except RuntimeError:
            raise H3Error("h3: baglanti kapandi") from None
        try:
            item = waiter.result(self._timeout)
        except concurrent.futures.TimeoutError:
            waiter.cancel()
            err = TimeoutError("h3: akis durdu (zaman asimi)")
            self._abort(err)
            raise err from None
        except concurrent.futures.CancelledError:
            self._eof = True
            return
        if isinstance(item, BaseException):
            self._error = item
            mark_broken(self.url)
            raise item
        if item is None:
            self._eof = True
            self._release()       # okur bitti: akis kaydi kapanabilir
        else:
            self._buf += item
            stream = self._stream
            if stream is not None:
                stream.take_buffered(len(item))
                if stream.conn is not None and stream.conn.paused:
                    self._maybe_resume()

    # --- akis yasami ---------------------------------------------------
    def _maybe_resume(self) -> None:
        """Tampon eridi: baglanti okumasi (geri basinc) acilabilir."""
        conn = self._stream.conn if self._stream is not None else None
        loop = self._loop
        if conn is None or not conn.paused or loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(_resume_if_dry, conn)
        except RuntimeError:
            pass

    def _release(self, reset: bool = False) -> None:
        """Akis kaydini birakir (olay dongusunde calisir)."""
        stream, loop = self._stream, self._loop
        if stream is None or stream.reader_done:
            return
        if loop is None or loop.is_closed():
            stream.reader_done = True
            return
        try:
            loop.call_soon_threadsafe(_release_stream, stream, reset)
        except RuntimeError:
            stream.reader_done = True

    def _fail(self, ex: BaseException) -> None:
        """Tasimayi basarisiz sayar: kaynagi bozar; okur hatayi kuyruktan
        (ya da dogrudan start() cagrisindan) alir."""
        if self._error is None:
            self._error = ex
        mark_broken(self.url)

    def _abort(self, ex: BaseException) -> None:
        self._fail(ex)
        self.close()

    async def _open(self) -> None:
        """Akisi baglantiya kaydeder, basliklari ve veriyi bekler."""
        stream = self._stream
        loop = asyncio.get_running_loop()
        if stream.queue is None:
            stream.queue = asyncio.Queue()
            stream.ready = loop.create_future()
            self._queue = stream.queue
        try:
            conn = await _conn_get(self.url, self.opt, self._target)
            if stream.reader_done:
                # start() zaman asimi ile akisi birakti: kaydetme, yoksa
                # kimsenin kapatmayacagi bir akis slotu kalirdi
                raise H3Error("h3: istek birakildi")
            stream.conn = conn
            stream.stream_id = conn.proto._quic.get_next_available_stream_id()
            conn.streams[stream.stream_id] = stream
            conn.last_used = _now()
            conn.proto.h3.send_headers(
                stream.stream_id,
                _request_headers(urlsplit(self.url), self.headers))
            conn.proto.transmit()
            await stream.ready
            if stream.error is not None:
                raise stream.error
            if not stream.got_headers:
                raise H3Error("h3: yanit basligi yok")
            self._status = stream.status
            self._headers = stream.headers
        except asyncio.CancelledError:
            _release_stream(stream, True)
            raise
        except BaseException as ex:
            _release_stream(stream, True)
            self._fail(ex)
            raise


class Response:
    """httpcli.probe/open_range'in tukettigi yanit arayuzu (urllib benzeri)."""

    def __init__(self, url: str, status: int,
                 headers: email.message.Message, session: _Session):
        self.url = url
        self.status = status
        self.headers = headers
        self._session = session
        self._closed = False

    def geturl(self) -> str:
        return self.url

    def read(self, n: int = -1) -> bytes:
        if self._closed:
            return b""
        return self._session.read(n)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def request(url: str, opt, headers: dict | None = None,
            timeout: float | None = None) -> Response:
    """TEK bir GET istegi (Range basligi headers icinde gelir) h3 uzerinden.

    Durum >= 400 ise govde okunmaz: urllib ile ayni davranis olsun diye
    HTTPError firlatilir (job.py 429/503 dallari bunu bekler). 3xx
    yonlendirme desteklenmez: kaynak bozuk isaretlenip H3Error verilir ki
    auto modda TCP (yonlendirmeyi izleyen yol) kullanilsin. TLS ayarlari
    `opt.tls` uzerinden uygulanir (CA + istemci sertifikasi)."""
    session = _Session(url, opt, headers, float(timeout or 30),
                       target=alt_svc_target(url, _insecure(opt)))
    try:
        status, msg = session.start()
        if status <= 0:
            session.close()
            mark_broken(url)
            raise H3Error("h3: gecersiz yanit")
        if status in _REDIRECT:
            session.close()
            mark_broken(url)
            raise H3Error("h3: yonlendirme desteklenmiyor")
        if status >= 400:
            session.close()
            raise urllib.error.HTTPError(url, status, f"HTTP {status}",
                                         msg, None)
        return Response(url, status, msg, session)
    except BaseException:
        session.close()
        raise
