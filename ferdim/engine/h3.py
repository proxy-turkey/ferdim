"""HTTP/3 (QUIC) tasimasi - istege bagli aioquic eklentisi.

Cekirdek stdlib'de kalir: aioquic yalnizca bu modul icinde ve yalnizca
gercekten HTTP/3 kullanilacaksa tembel yuklenir (modul importu aioquic
gerektirmez). Kapsam bilincli olarak dardir: yalnizca probe ve Range
akisi h3 uzerinden gider; yan dosyalar (fetch_small) ve HLS/DASH
segmentleri TCP'de kalir.

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

Asenkron aioquic istemcisi baglanti basina ayri bir arka plan is
parcaciginda/olay dongusunde kosar; veri asyncio kuyrugundan bloklayan
okumaya karsi sinirli bir tamponla koprulenir, her okuma stall
zaman asimina tabidir. Alt-Svc duyurulan host:port'a baglanilir ama
`:authority` ve TLS SNI kaynagin kendi adresidir.

Baglanti yeniden kullanimi (oturum bileti / origin basina havuz)
bilincli olarak yok: her Range kendi QUIC el sikismasini yapar; kazanc
olculmedi, karmasiklik ve kaynak sizintisi riski yuksek.
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

MODES = ("off", "auto", "force")
BROKEN_SECS = 600.0             # h3 hatasi sonrasi TCP'de kalma suresi
ALT_SVC_DEFAULT_MAX_AGE = 86400.0
DATA_QUEUE = 8                  # olay dongusunden okura tampon (parca)
JOIN_SECS = 3.0
CONNECT_SECS = 5.0              # el sikismasi ust siniri (stall'dan bagimsiz)
DRAIN_SECS = 60.0               # akis bitti: okur kuyrugu bosaltmak icin sure

# h3'te baglantiya ozgu (yasak) basliklar gonderilemez
_HOP = frozenset({"connection", "keep-alive", "proxy-connection", "host",
                  "transfer-encoding", "upgrade", "te"})
_REDIRECT = frozenset({301, 302, 303, 307, 308})

_API = None                     # aioquic sembolleri (tembel)
_AVAILABLE: bool | None = None  # available() sonucu (negatif dahil) onbellek
_PROTO = None                   # QuicConnectionProtocol alt sinifi (tembel)
# origin -> (host, port, bitis); host "" = kaynagin kendi adresi
_ALT_SVC: dict[str, tuple[str, int, float]] = {}
_BROKEN: dict[str, float] = {}                # origin -> bitis zamani
_LOCK = threading.Lock()


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
        from aioquic.quic.events import ConnectionTerminated
        _API = {
            "connect": connect,
            "QuicConnectionProtocol": QuicConnectionProtocol,
            "H3Connection": H3Connection,
            "H3_ALPN": H3_ALPN,
            "DataReceived": DataReceived,
            "HeadersReceived": HeadersReceived,
            "QuicConfiguration": QuicConfiguration,
            "ConnectionTerminated": ConnectionTerminated,
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
    """Sema + ana makine + port (Alt-Svc/bozuk kaynak anahtari)."""
    p = urlsplit(url)
    port = p.port or (443 if p.scheme == "https" else 80)
    return f"{p.scheme}://{(p.hostname or '').lower()}:{port}"


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


def remember_alt_svc(url: str, value: str | None) -> bool:
    """TCP yanitindaki Alt-Svc basligini onbellege yazar (ma=0 siler)."""
    parsed = parse_alt_svc(value)
    if parsed is None:
        return False
    alt_host, port, max_age = parsed
    key = origin(url)
    with _LOCK:
        if max_age <= 0:
            _ALT_SVC.pop(key, None)
            return False
        _ALT_SVC[key] = (alt_host, port, _now() + max_age)
    return True


def alt_svc_target(url: str) -> tuple[str, int] | None:
    """Onbellekteki h3 hedefi (host, port); yoksa/suresi dolduysa None.

    host bos ise kaynagin kendi ana makinesine baglanilir."""
    key = origin(url)
    with _LOCK:
        item = _ALT_SVC.get(key)
        if item is None:
            return None
        if item[2] <= _now():
            _ALT_SVC.pop(key, None)
            return None
        return item[0], item[1]


def mark_broken(url: str) -> None:
    """h3 denemesi basarisiz: kaynak BROKEN_SECS boyunca TCP'de kalir."""
    with _LOCK:
        _BROKEN[origin(url)] = _now() + BROKEN_SECS


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
    """Testler icin: Alt-Svc ve bozuk kaynak onbellegini temizler."""
    with _LOCK:
        _ALT_SVC.clear()
        _BROKEN.clear()


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
    return not broken(url) and alt_svc_target(url) is not None


# --------------------------------------------------------- istek/yanit
def _authority(parts: SplitResult) -> str:
    host = parts.hostname or ""
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
            self.events: asyncio.Queue = asyncio.Queue()

        def quic_event_received(self, event):
            if isinstance(event, api["ConnectionTerminated"]):
                self.events.put_nowait(event)
                return
            for h3_event in self.h3.handle_event(event):
                self.events.put_nowait(h3_event)

    _PROTO = Proto
    return Proto


class _Session:
    """Tek h3 istegi: arka planda olay dongusu, bloklayan okuma koprusu.

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
        self._drain: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task = None
        self._thread: threading.Thread | None = None
        self._queue: asyncio.Queue | None = None
        self._ready: concurrent.futures.Future | None = None
        self._status = 0
        self._headers: email.message.Message | None = None
        self._got_headers = False
        self._error: BaseException | None = None
        self._buf = bytearray()
        self._eof = False
        self._closed = False

    # --- dis arayuz ---------------------------------------------------
    def start(self) -> tuple[int, email.message.Message]:
        """Baglantiyi kurar ve yanit basliklarini bekler (zaman asimli)."""
        self._ready = concurrent.futures.Future()
        self._thread = threading.Thread(
            target=self._run, name="ferdim-h3", daemon=True)
        self._thread.start()
        try:
            self._ready.result(self._connect_timeout)
        except concurrent.futures.TimeoutError:
            self.close()
            mark_broken(self.url)
            raise TimeoutError("h3: yanit basligi gelmedi") from None
        if self._error is not None:
            raise self._error
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
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._cancel)
            except RuntimeError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(JOIN_SECS)

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
            raise item
        if item is None:
            self._eof = True
            self._signal_drained()    # olay dongusu artik kapanabilir
        else:
            self._buf += item

    def _signal_drained(self) -> None:
        loop, drain = self._loop, self._drain
        if loop is not None and drain is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(drain.set)
            except RuntimeError:
                pass

    # --- arka plan ----------------------------------------------------
    def _run(self) -> None:
        if self._closed:
            return
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._task = loop.create_task(self._request())
            if self._closed:              # close() yarisi: istek baslamasin
                self._task.cancel()
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except BaseException as ex:            # noqa: BLE001 - okura iletilir
            self._fail(ex)
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    def _cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _fail(self, ex: BaseException) -> None:
        """Tasimayi basarisiz sayar: kaynagi bozar, okuru uyandirir."""
        if self._error is None:
            self._error = ex
        mark_broken(self.url)
        ready = self._ready
        if ready is not None and not ready.done():
            ready.set_exception(ex)
        loop, queue = self._loop, self._queue
        if loop is not None and queue is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(_offer, queue, ex)
            except RuntimeError:
                pass

    def _abort(self, ex: BaseException) -> None:
        self._fail(ex)
        self.close()

    async def _request(self) -> None:
        api = _load()
        self._queue = asyncio.Queue(maxsize=DATA_QUEUE)
        self._drain = asyncio.Event()
        parts = urlsplit(self.url)
        host = parts.hostname or ""
        port = parts.port or 443
        if self._target is not None:      # Alt-Svc: duyurulan host:port
            host = self._target[0] or host
            port = self._target[1]
        conf = api["QuicConfiguration"](
            is_client=True,
            alpn_protocols=list(api["H3_ALPN"]),
            # SNI/ana makine dogrulamasi her zaman KAYNAGIN adiyla
            server_name=parts.hostname or host,
            verify_mode=ssl.CERT_NONE if getattr(self.opt, "insecure", False)
            else ssl.CERT_REQUIRED,
        )
        headers = _request_headers(parts, self.headers)
        async with api["connect"](host, port, configuration=conf,
                                  create_protocol=_protocol_class()) as client:
            stream_id = client._quic.get_next_available_stream_id()
            client.h3.send_headers(stream_id, headers)
            client.transmit()
            while True:
                event = await client.events.get()
                if isinstance(event, api["HeadersReceived"]):
                    if event.stream_id != stream_id:
                        continue
                    if not self._got_headers:      # izleyen HEADERS = trailer
                        self._got_headers = True
                        self._status = _status_of(event.headers)
                        self._headers = _message(event.headers)
                        if self._ready is not None and not self._ready.done():
                            self._ready.set_result(True)
                    if event.stream_ended:
                        await self._queue.put(None)
                        break
                elif isinstance(event, api["DataReceived"]):
                    if event.stream_id != stream_id:
                        continue
                    if event.data:
                        await self._queue.put(event.data)
                    if event.stream_ended:
                        await self._queue.put(None)
                        break
                elif isinstance(event, api["ConnectionTerminated"]):
                    raise ConnectionError(
                        f"h3: baglanti kapandi (kod {event.error_code})")
            # Akis bitti ama kuyrukta/tamponda veri olabilir: dongu, okur
            # sentinel'i gorene kadar YASAMALI. Erken kapatma kuyruktaki
            # kuyrugu kaybettirip sahte stall/H3Error uretiyordu.
            try:
                await asyncio.wait_for(self._drain.wait(), DRAIN_SECS)
            except asyncio.TimeoutError:
                pass


def _offer(queue: asyncio.Queue, item) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        pass


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
    auto modda TCP (yonlendirmeyi izleyen yol) kullanilsin.
    """
    session = _Session(url, opt, headers, float(timeout or 30),
                       target=alt_svc_target(url))
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
