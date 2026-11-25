"""urllib tabanli HTTP yardimcilari: probe, Range akisi, opener kurulumu.

Turbo modu icin baglantilara buyuk SO_RCVBUF uygulanir: cekirdek her
baglanti icin daha derin bir alma kuyrugu tutar, hat daha hizli dolar.

HTTP/3 acikken (DlOptions.http3) istekler h3 uzerinden gider: `_open_any`
hem Range akisini hem de kucuk metin cekimini (yan dosya, m3u8/MPD,
HLS-DASH segmenti) ayni secimden gecirir. `_make_opener` urllib opener'ini
saran h3 farkinda bir nesne doner (proxy/TLS yolu korunur), boylece
hls.py/dash.py/segments.py imza degistirmeden h3'ten gecer.

Kimlik kapsami: Authorization, Cookie ve kullanicinin ozel basliklari
YALNIZCA kimlik sahibi kokenine (sema+ana makine+port) gider. Iki yerde
uygulanir: `DlOptions.headers_for` (ayna baska koken ise dusurur) ve
`ScopedRedirectHandler` (30x zincirinde yabanci koken ya da https->http
dususunde dusurur). http->https yukseltmesi (80 -> 443) ayni koken sayilir:
kimlik korunur. Kimlik OLMAYAN basliklar her atlama da kalir: Referer
(CDN'lerin hotlink denetimi) ve If-Range (dogrulayici; yonlendirme hedefi
dosyayi sunan kaynak oldugu icin devam dogrulamasi orada gerekir).
Proxy-Authorization kimlik sayilir ve tasinmaz: gecitli atlamada urllib'in
ProxyHandler'i onu proxy URL'inden yeniden ekler.

Keep-alive havuzu (ROADMAP 1.13): dogrudan (gecitsiz, socks'suz) http/https
istekleri surec genelinde bir bosta baglanti havuzundan gecer. Govdesi TAM
okunmus ve sunucu kapatmayi istememis HTTP/1.1 baglantisi anahtari basina
saklanir; yarim okunmus bir soket asla geri konmaz. Boylece kisa Range
istekleri baglanti/TLS el sikismasi maliyetini tekrar odemez. TLS
oturumlari anahtar basina saklanip yeni baglantida `wrap_socket(session=)`
ile sunulur (yeniden kullanim = daha kisa el sikismasi). Proxy'li, SOCKS'lu
ve h3 istekleri eski yolda kalir: orada baglanti kurulumu gecide/tunele
baglidir.
"""
from __future__ import annotations

import base64
import functools
import http.client
import os
import socket
import ssl
import stat
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from urllib.parse import urljoin, urlsplit

from ..profiles import normalise_host
from . import h3, socks

RCVBUF = 1 << 21  # 2 MB (cekirdek rmem_max ile kisitlanir, sorun degil)


def _tune(sock: socket.socket) -> None:
    """Baglanti soketine turbo tamponunu uygular (stdlib ile ayni)."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
    except OSError:
        pass


# ------------------------------------------------------------------ TLS
# HTTP, h3 ve FTPS ayni TLS ayarlarini paylasir: ozel CA paketi, istemci
# sertifikasi (mTLS) ve asgari TLS surumu. Tek kaynak burasidir; her
# tuketici `ssl_context`/`TlsConfig` uzerinden ayni degeri gorur.
class TlsConfigError(Exception):
    """TLS ayari kullanilamaz (dosya yok/okunamiyor). code='tls_config'."""

    code = "tls_config"


# TLS dosyasi ust siniri: sertifika/anahtar dosyalari kucuktur; buyuk bir
# dosyayi (ya da bir aygit/FIFO'yu) okumak saldiri yuzeyi olurdu.
MAX_TLS_FILE = 1 << 20          # 1 MiB


def tls_file_path(value) -> str:
    """TLS dosyasini dogrular ve gercek (realpath) yolunu dondurur.

    Kural: mutlak yol, duzenli dosya (FIFO/aygit degil), en fazla 1 MiB,
    sahibi bu kullanici ya da root, grup/diger yazma izni yok. Gecersizse
    bos dize. Boylece HTTP API'den ya da elle yazilmis config.json'dan
    gelen bir yol aygit okumasina ya da baskasinin yazabildigi bir
    sertifikaya donusemez. Acmadan once `stat` ile tur denetlenir.
    """
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw or "\x00" in raw:
        return ""
    p = os.path.expanduser(raw)
    if not os.path.isabs(p):
        return ""
    try:
        real = os.path.realpath(p)
        st = os.stat(real)
    except (OSError, ValueError):
        return ""
    if not stat.S_ISREG(st.st_mode):
        return ""
    if st.st_size > MAX_TLS_FILE:
        return ""
    if st.st_uid not in (os.geteuid(), 0):
        return ""
    if st.st_mode & 0o022:          # grup/diger yazma: degistirilebilir
        return ""
    try:
        fd = os.open(real, os.O_RDONLY)
    except OSError:
        return ""
    os.close(fd)
    return real


def _file_signature(path: str):
    """Dosyanin (realpath, mtime_ns, boyut) imzasi; yoksa None."""
    if not path:
        return None
    try:
        real = os.path.realpath(path)
        st = os.stat(real)
    except (OSError, ValueError):
        return (path, -1, -1)
    return (real, st.st_mtime_ns, st.st_size)


@dataclass(frozen=True)
class TlsConfig:
    """TLS ayarlari (config anahtarlarinin tasiyicisi)."""

    insecure: bool = False
    ca_file: str = ""         # ek CA paketi (sistem koklerine EKLENIR)
    client_cert: str = ""     # mTLS istemci sertifikasi
    client_key: str = ""      # bos ise anahtar sertifika dosyasindadir
    min_version: str = ""     # "" varsayilan | "1.2" | "1.3"

    @classmethod
    def from_cfg(cls, cfg: dict, insecure: bool = False) -> "TlsConfig":
        """config sozlugunden TLS ayarlari (verify_tls dahil)."""
        get = cfg.get if isinstance(cfg, dict) else (lambda _k, _d=None: _d)
        return cls(
            insecure=bool(insecure or not get("verify_tls", True)),
            ca_file=str(get("tls_ca_file") or ""),
            client_cert=str(get("tls_client_cert") or ""),
            client_key=str(get("tls_client_key") or ""),
            min_version=str(get("tls_min_version") or ""),
        )

    @classmethod
    def from_opt(cls, opt) -> "TlsConfig":
        """DlOptions/benzeri nesneden; `insecure` her zaman dikkate alinir."""
        t = getattr(opt, "tls", None)
        if not isinstance(t, cls):
            t = cls()
        if getattr(opt, "insecure", False) and not t.insecure:
            t = replace(t, insecure=True)
        return t

    def validate(self) -> None:
        """Dosyalar mutlak, duzenli ve guvenli mi; anahtar sertifikasiz olmaz."""
        for path in (self.ca_file, self.client_cert, self.client_key):
            if path and not tls_file_path(path):
                raise TlsConfigError(f"TLS dosyasi okunamadi: {path}")
        if self.client_key and not self.client_cert:
            raise TlsConfigError(
                "TLS istemci anahtari sertifika olmadan kullanilamaz")

    def fingerprint(self) -> tuple:
        """TLS ayar izi: min surum + her dosyanin (mtime_ns, boyut) imzasi.

        SSL baglami onbellegi ve h3 baglanti havuzu bunu anahtara katar:
        CA/sertifika dosyasi degistirilince eski baglanti yeniden
        kullanilmaz."""
        return (self.min_version,
                tuple(_file_signature(p) for p in
                      (self.ca_file, self.client_cert, self.client_key)))

    def apply_quic(self, conf) -> None:
        """aioquic QuicConfiguration'a CA + istemci sertifikasi uygular.

        QUIC yalniz TLS 1.3 kullanir: `min_version` icin ayar gerekmez.
        aioquic'te `load_verify_locations` varsayilan (certifi) depoyu
        DEGISTIRIR; HTTP yolunda ise CA paketi sistem koklerine eklenir.
        """
        self.validate()
        if self.ca_file:
            conf.load_verify_locations(cafile=self.ca_file)
        if self.client_cert:
            conf.load_cert_chain(self.client_cert, self.client_key or None)


@functools.lru_cache(maxsize=8)
def _context_for(settings: TlsConfig, signature: tuple) -> ssl.SSLContext:
    """Ayarlardan SSL baglami uretir (ayar + dosya imzasi basina onbellek).

    `signature` yalnizca onbellek anahtarinin parcasi; dosya degistiginde
    (mtime_ns/boyut) yeni bir baglam uretilir, eski baglam kullanilmaz."""
    settings.validate()
    ctx = ssl.create_default_context()
    if settings.ca_file:
        try:
            # ek CA: create_default_context'in yukledigi sistem kokleri kalir
            ctx.load_verify_locations(cafile=settings.ca_file)
        except (ssl.SSLError, OSError) as ex:
            raise TlsConfigError(
                f"TLS CA paketi yuklenemedi: {settings.ca_file}") from ex
    if settings.client_cert:
        try:
            ctx.load_cert_chain(settings.client_cert,
                                settings.client_key or None)
        except (ssl.SSLError, OSError) as ex:
            raise TlsConfigError(
                "TLS istemci sertifikasi yuklenemedi: "
                f"{settings.client_cert}") from ex
    if settings.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    ver = {"1.2": ssl.TLSVersion.TLSv1_2,
           "1.3": ssl.TLSVersion.TLSv1_3}.get(settings.min_version)
    if ver is not None:
        ctx.minimum_version = ver
    return ctx


def tls_settings(opt) -> TlsConfig:
    """DlOptions (veya benzeri) -> TlsConfig; TlsConfig aynen doner."""
    return opt if isinstance(opt, TlsConfig) else TlsConfig.from_opt(opt)


def ssl_context(opt) -> ssl.SSLContext:
    """Paylasilan TLS baglami: HTTP/FTPS ayni ayardan ayni nesneyi alir.

    Onbellek anahtari dosyalarin (mtime_ns, boyut) imzasini da tasir:
    CA/sertifika degistirilince yeni baglam kurulur."""
    settings = tls_settings(opt)
    return _context_for(settings, settings.fingerprint())


# ------------------------------------------------------- kimlik kapsami
# Kimlik sahibi koken disina gonderilirken YALNIZ bu basliklar kalir
# (varsayilan olarak REDDET). Liste = kimlik TASIMAYAN basliklar: hepsi
# FerDim'in/urllib'in kendi urettigi ya da herkese acik baglam bilgisi.
#  - Referer: kimlik degil; CDN'ler yonlendirme sonrasi hotlink denetimi
#    icin bakar (tarayicilar da kokenler arasi gonderir).
#  - If-Range: kimlik degil, DOGRULAYICI. Ayna/yonlendirme hedefi dosyayi
#    fiilen sunan kaynak olabilir (surum sayfasi -> CDN); burada atilirsa
#    devam dogrulamasi tam da yaygin durumda kaybolur ve degismis dosya
#    karisik devam ederdi.
#  - Proxy-Authorization BURADA YOK: gecidin kimligi hedefe gitmez. Gecitli
#    atlamada urllib'in ProxyHandler'i onu proxy URL'inden yeniden ekler;
#    statik bir baslik yabanci kokenin sunucusuna sizardi.
_OPEN_HEADERS = frozenset({
    "host", "user-agent", "accept", "accept-encoding", "connection",
    "range", "if-range", "referer",
})


def origin_of(url: str) -> tuple:
    """Koken: (sema, ana makine, port) - karsilastirma icin normalised.

    Sema ve ana makine kucuk harfe indirilir, port semaya gore varsayilanla
    doldurulur (http 80, https 443). Cozulemeyen URL icin ("", "", 0);
    boyle bir deger hicbir kokenle ayni sayilmaz."""
    try:
        p = urlsplit(str(url or ""))
        scheme = (p.scheme or "").lower()
        host = normalise_host(p.hostname or "")
        port = p.port
    except ValueError:
        return ("", "", 0)
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme, 0)
    return (scheme, host, port)


def same_origin(origin: tuple, url: str) -> bool:
    """`origin` bu URL'in kokeniyle ayni mi?

    Farkli ana makine, farkli sema (https->http dususu dahil) ya da farkli
    port = farkli koken. Istisna: http (80) -> https (443) YUKSELTMESI ayni
    koken sayilir; kimlik korunur (hedef makine ayni, tasima daha guvenli).
    Ters yon (https->http) kimligi atar. Gecersiz/eksik koken hicbir zaman
    ayni sayilmaz: kimlik sizmaz."""
    if not origin or origin[0] not in ("http", "https"):
        return False
    target = origin_of(url)
    if origin == target:
        return True
    return (origin[0] == "http" and origin[2] == 80
            and target[0] == "https" and target[2] == 443
            and origin[1] == target[1])


def strip_credentials(headers: dict) -> dict:
    """Kimlik basliklarini atar: Authorization, Cookie, kullanicinin ozel
    basliklari ve Proxy-Authorization. Kimlik OLMAYANLAR kalir: User-Agent,
    Accept-Encoding, Connection, Range, If-Range, Referer (bak:
    `_OPEN_HEADERS`). Gecitli atlamada proxy kimligini urllib'in ProxyHandler
    kendisi proxy URL'inden ekler; statik bir kopya hedefe sizardi."""
    return {k: v for k, v in headers.items()
            if str(k).strip().lower() in _OPEN_HEADERS
}


class FastHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        _tune(self.sock)


class FastHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        _tune(self.sock)


def _socks_http_conn(proxy: str):
    """SOCKS5 tuneli uzerinden duz HTTP baglantisi."""

    class Conn(http.client.HTTPConnection):
        def connect(self):
            self.sock = socks.open_tunnel(proxy, self.host, self.port,
                                          self.timeout)
            _tune(self.sock)

    return Conn


def _socks_https_conn(proxy: str):
    """Tunel uzerinden TLS: SNI ve sertifika dogrulamasi hedef adiyla."""

    class Conn(http.client.HTTPSConnection):
        def connect(self):
            raw = socks.open_tunnel(proxy, self.host, self.port,
                                    self.timeout)
            try:
                self.sock = self._context.wrap_socket(
                    raw, server_hostname=self.host)
            except BaseException:
                raw.close()
                raise
            _tune(self.sock)

    return Conn


class SocksHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, proxy: str):
        super().__init__()
        self._proxy = proxy

    def http_open(self, req):
        return self.do_open(_socks_http_conn(self._proxy), req)


class SocksHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, proxy: str, opt: "DlOptions"):
        super().__init__()
        self._proxy = proxy
        self._opt = opt
        self._ctx_ready = False

    def https_open(self, req):
        # baglam yalniz HTTPS isteginde kurulur: TLS ayari bozuksa duz
        # http isi etkilenmez
        if not self._ctx_ready:
            self._context = ssl_context(self._opt)
            self._ctx_ready = True
        return self.do_open(_socks_https_conn(self._proxy), req,
                            context=self._context)


class FastHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(FastHTTPConnection, req)


class FastHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, opt: "DlOptions"):
        super().__init__()
        self._opt = opt
        self._ctx_ready = False

    def https_open(self, req):
        # baglam verilmezse --insecure/verify_tls=false sessizce yok sayilir
        if not self._ctx_ready:
            self._context = ssl_context(self._opt)
            self._ctx_ready = True
        return self.do_open(FastHTTPSConnection, req,
                            context=self._context)


class ScopedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """30x zinciri: kimlik basliklari yalnizca ILK istegin kokeninde kalir.

    stdlib handler'i govde disi butun basliklari (Authorization, Cookie,
    kullanicinin ozel basliklari, Proxy-Authorization) hedefe kopyalar:
    yabanci bir koken 30x ile parolayi/cerezi toplayabilirdi. Burada hedef
    koken ilk istegin kokeninden farkliysa (ana makine/port; https->http
    dususu dahil) kimlik basliklari atilir; kimlik OLMAYANLAR kalir: Range,
    If-Range (dogrulayici) ve Referer (`_OPEN_HEADERS`). http->https
    yukseltmesi (80 -> 443) ayni koken sayilir: kimlik korunur.

    Zincir boyunca ILK koken tasinir (`_ferdim_origin`): A->B->A donusunde
    kimlik geri gelmez, cunku A istegi B isteginin (kimliksiz) basliklari
    uzerine kurulur. Bir kez atilan kimlik o istek icin atilmis kalir."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        origin = getattr(req, "_ferdim_origin", None) \
            or origin_of(getattr(req, "full_url", ""))
        new._ferdim_origin = origin
        if same_origin(origin, newurl):
            return new
        new.headers = strip_credentials(new.headers)
        return new


# --------------------------------------------------------- baglanti havuzu
# Range istekleri kisa: istek basina yeni TCP + TLS el sikismasi, indirilen
# birkac yuz kilobayttan pahali. Havuz govdesi TAM okunmus ve sunucu
# kapatmayi istememis HTTP/1.1 baglantilarini anahtar basina tutar; yarim
# okunmus bir soket geri konmaz (akis karisirdi).
#
# Kapsam: yalnizca DOGRUDAN (proxy'siz, socks'suz) http/https istekleri.
# Proxy/SOCKS/h3 yollari eski urllib/h3 yolunda kalir (bkz. `_poolable`).
POOL_TTL = 30.0          # bosta en fazla bu kadar saniye
POOL_PER_KEY = 32        # anahtar basina bosta baglanti
POOL_MAX = 256           # toplam bosta baglanti


def _pool_max() -> int:
    """Toplam bosta baglanti tavani: fd butcesinin sekizde biri (en cok 256).

    Havuzdaki her baglanti bir fd tutar; indirme isleri ayni butceyi
    kullandigi icin tavan RLIMIT_NOFILE'dan turetilir."""
    try:
        import resource
        soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:
        return POOL_MAX
    if not soft or soft <= 0 or soft == getattr(resource, "RLIM_INFINITY", -1):
        return POOL_MAX
    return max(16, min(POOL_MAX, int(soft) // 8))


class _Idle:
    """Havuzda bekleyen baglanti ve birakilma ani."""

    __slots__ = ("conn", "ts")

    def __init__(self, conn, ts: float):
        self.conn = conn
        self.ts = ts

    def close(self) -> None:
        if _conn_open(self.conn):
            self.conn.close()


def _conn_open(conn) -> bool:
    """Baglanti hala bir soket tutuyor mu (http.client kapattiysa hayir)."""
    return conn is not None and getattr(conn, "sock", None) is not None


class _ConnPool:
    """Anahtar basina bosta baglanti havuzu (is parcacigi guvenli).

    `take` bostaki EN YENI baglantiyi verir (sicak soket once gelir),
    `put` geri koyar. TTL'i gecen baglantilar hem tembel (take/put) hem de
    havuz bos degilken calisan tek bir hafif toplayici ile kapatilir:
    sunucu tarafinda yarim kalan baglanti ve fd birikmez."""

    def __init__(self):
        self._lock = threading.Lock()
        self._idle: dict = {}
        self._count = 0
        self._sessions: dict = {}
        self._wake = threading.Event()
        self._reaper: threading.Thread | None = None
        self.ttl = POOL_TTL
        self.per_key = POOL_PER_KEY
        self.max = _pool_max()

    # -- baglanti ----------------------------------------------------
    def take(self, key):
        """Bostaki en yeni saglam baglanti; yoksa None."""
        now = time.monotonic()
        with self._lock:
            items = self._idle.get(key)
            if not items:
                return None
            item = items.pop()
            self._count -= 1
            if not items:
                self._idle.pop(key, None)
        if now - item.ts > self.ttl or not _conn_open(item.conn):
            item.close()
            return None
        return item.conn

    def put(self, key, conn) -> None:
        """Tam okunmus baglantiyi havuza birakir (tavanlari uygular)."""
        if not _conn_open(conn):
            return
        drop = []
        with self._lock:
            items = self._idle.setdefault(key, [])
            items.append(_Idle(conn, time.monotonic()))
            self._count += 1
            while len(items) > self.per_key:
                self._count -= 1
                drop.append(items.pop(0))
            if self._count > self.max:
                self._count -= 1
                drop.append(items.pop())
            if not items:
                self._idle.pop(key, None)
            self._ensure_reaper()
        for item in drop:
            item.close()

    # -- TLS oturumu -------------------------------------------------
    def session(self, key):
        """Anahtarin son TLS oturumu (yoksa None)."""
        with self._lock:
            return self._sessions.get(key)

    def store_session(self, key, session) -> None:
        """Son oturumu anahtara yazar (en fazla 64 anahtar tutulur)."""
        if session is None:
            return
        with self._lock:
            if key not in self._sessions and len(self._sessions) >= 64:
                self._sessions.pop(next(iter(self._sessions)))
            self._sessions[key] = session

    # -- temizlik ----------------------------------------------------
    def clear(self, keep_sessions: bool = False) -> None:
        """Bosta baglantilari kapatir; oturumlar korunabilir."""
        with self._lock:
            items = [item for group in self._idle.values() for item in group]
            self._idle.clear()
            self._count = 0
            if not keep_sessions:
                self._sessions.clear()
        self._wake.set()
        for item in items:
            item.close()

    def stats(self) -> dict:
        with self._lock:
            return {"idle": self._count, "keys": len(self._idle),
                    "sessions": len(self._sessions)}

    # -- toplayici ---------------------------------------------------
    def _ensure_reaper(self) -> None:
        """Bosta baglanti varsa tek temizleyici is parcacigi (kilitli)."""
        if self._reaper is not None and self._reaper.is_alive():
            return
        self._reaper = threading.Thread(target=self._reap,
                                        name="ferdim-conn-pool", daemon=True)
        self._reaper.start()

    def _reap(self) -> None:
        while True:
            dead = []
            now = time.monotonic()
            with self._lock:
                for key in list(self._idle):
                    items = self._idle[key]
                    while items and now - items[0].ts > self.ttl:
                        dead.append(items.pop(0))
                        self._count -= 1
                    if not items:
                        self._idle.pop(key, None)
                stop = not self._count
                if stop:
                    self._reaper = None
            for item in dead:
                item.close()
            if stop:
                return
            self._wake.wait(max(1.0, self.ttl / 3.0))
            self._wake.clear()


_POOL = _ConnPool()


def _pool() -> _ConnPool:
    return _POOL


def close_pool(keep_sessions: bool = False) -> None:
    """Bosta baglantilari kapatir (testler ve kapanis); oturum korunabilir."""
    _POOL.clear(keep_sessions)


def pool_stats() -> dict:
    """Havuz sayaclari (gozlem/test): idle, keys, sessions."""
    return _POOL.stats()


class PoolTLSConnection(http.client.HTTPSConnection):
    """HTTPS baglantisi: son TLS oturumu ile el sikismasini kisaltir.

    Verilen oturum sunucuda gecerli degilse (ya da surum uyusmuyorsa) tam
    el sikismasi yapilir: resumption bir hizlandirma, kosul degildir.
    TLS 1.3 bileti el sikismasindan sonra geldigi icin `tls_session` ilk
    yanit okunduktan sonra dolar."""

    def __init__(self, host, port=None, *, context, session=None,
                 timeout=None):
        super().__init__(host, port, timeout=timeout, context=context)
        self._session = session
        self.tls_session = None
        self.session_reused = False

    def connect(self):
        if self._session is not None:
            try:
                self._handshake(self._session)
                return
            except (ssl.SSLError, ValueError, TypeError):
                pass                  # oturum kabul edilmedi: tam el sikismasi
        self._handshake(None)

    def _handshake(self, session) -> None:
        raw = socket.create_connection((self.host, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(
                raw, server_hostname=self.host, session=session)
        except BaseException:
            raw.close()
            raise
        self.session_reused = bool(getattr(self.sock, "session_reused", False))
        try:
            self.tls_session = self.sock.session
        except (ssl.SSLError, ValueError):
            self.tls_session = None
        _tune(self.sock)


# --------------------------------------------------- havuzlu istek yolu
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 10        # urllib ile ayni
MAX_REPEATS = 4           # ayni hedefe bu kadar gidilirse dongu sayilir
# Bosta duran soket sunucu tarafinda kapanmis olabilir: gonderim/yanit
# asamasinda gorulen bu hatalar TAZE baglantiyla bir kez yinelenir.
_STALE_ERRORS = (ConnectionResetError, ConnectionAbortedError,
                 BrokenPipeError, http.client.RemoteDisconnected,
                 http.client.BadStatusLine, http.client.CannotSendRequest)


def _stale(ex: BaseException) -> bool:
    """Bosta soketin olu cikmasi mi? Zaman asimi/sertifika hatasi degil."""
    return isinstance(ex, _STALE_ERRORS) and not isinstance(ex, TimeoutError)


def _endpoint(url: str):
    """(sema, ana makine, port, hedef) ya da bu yolun kapsami disinda None.

    Ana makine IDNA/A-label bicimine normalisedir (SNI ve Host basligi
    ayni yazimi gorur). ASCII disi hedef havuz yolundan cikarilir: eski
    opener ayni istegi zaten reddeder/ kodlar."""
    try:
        parts = urlsplit(str(url or ""))
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            return None
        host = normalise_host(parts.hostname or "")
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    if not host:
        return None
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    if not target.isascii() or not host.isascii():
        return None
    return scheme, host, port, target


def _env_proxy(scheme: str, host: str) -> bool:
    """Ortam degiskenleri bu istek icin gecit istiyor mu (urllib davranisi)?"""
    try:
        if urllib.request.proxy_bypass(host):
            return False
        proxies = urllib.request.getproxies()
    except Exception:
        return True               # bilinmiyorsa eski (opener) yolu kullan
    return bool(proxies.get(scheme) or proxies.get("all"))


def _poolable(url: str, opt: DlOptions) -> bool:
    """Istek dogrudan mi gidiyor: havuz yolu kullanilabilir mi?

    `opt.proxy`, ortam degiskeni gecidi, SOCKS tuneli ve h3 secili
    istekler havuz disinda kalir; onlar eski yoldan gider."""
    ep = _endpoint(url)
    if ep is None:
        return False
    if getattr(opt, "proxy", ""):
        return False
    if h3.use_h3(url, opt):
        return False
    return not _env_proxy(ep[0], ep[1])


def _send_headers(headers: dict) -> dict:
    """Istek basliklari: `Connection` daima keep-alive.

    `DlOptions.base_headers` baglantiyi kapatmayi ister (tek istek yolu);
    havuzda bu yeniden kullanimi imkansiz kilardi. Kullanici baska bir
    deger verdiyse bile havuz yolu keep-alive gonderir: sunucu kapatmak
    isterse yanitta `Connection: close` ile soyler ve soket havuza donmez."""
    out = {k: v for k, v in (headers or {}).items()
           if str(k).strip().lower() != "connection"}
    out["Connection"] = "keep-alive"
    return out


def _safe_close(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def _discard(conn) -> None:
    """Yarim okunmus soketi sessizce kapatir (RST uretmeden).

    Okunmamis bayt varken `close()` karsi tarafa RST gonderir; sunucu bunu
    baglanti hatasi olarak gorur. Kapanistan once tampondaki baytlari
    bosaltmak normal kapanis (FIN) birakir. Okuma non-blocking'dir: veri
    gelmiyorsa beklemeden kapatilir, `close()` asla stall suresi kadar
    bloklamaz."""
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.setblocking(False)
            while sock.recv(65536):
                pass
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            pass
    _safe_close(conn)


class PooledResponse:
    """Havuzlu yanit: urllib/http.client yanit arayuzu.

    `read`/`close`/`headers`/`geturl` urllib yanitiyla ayni sozlesmededir
    (job.py, probe, fetch_small ve segments bu arayuzu kullanir). Govde
    TAM okununca baglanti havuza doner; `close()` govde bitmeden
    cagrilirsa soket atilir: yarim okunmus baglanti yeniden kullanilmaz."""

    def __init__(self, url: str, resp, key: tuple, conn):
        self.url = url
        self.status = int(getattr(resp, "status", 0) or 0)
        self.code = self.status
        self.reason = getattr(resp, "reason", "") or ""
        self.msg = self.reason
        self.headers = resp.headers
        self._resp = resp
        self._key = key
        self._conn = conn
        self._done = False

    # urllib benzeri arayuz ------------------------------------------
    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return self.status

    def info(self):
        return self.headers

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def getheaders(self) -> list:
        return list(self.headers.items())

    def readable(self) -> bool:
        return True

    @property
    def fp(self):
        """Govde tamponu: `response_socket` soketi buradan bulur (TCP_INFO)."""
        return getattr(self._resp, "fp", None)

    def read(self, amt=None) -> bytes:
        if self._done:
            return b""
        try:
            data = self._resp.read(amt)
        except BaseException:
            self._drop()
            raise
        if self._complete():
            self._recycle()
        return data

    def readline(self, limit: int = -1) -> bytes:
        if self._done:
            return b""
        try:
            data = self._resp.readline(limit)
        except BaseException:
            self._drop()
            raise
        if self._complete():
            self._recycle()
        return data

    def close(self) -> None:
        if self._done:
            return
        try:
            self._resp.close()
        except Exception:
            pass
        self._drop()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # govde sonu -----------------------------------------------------
    def _complete(self) -> bool:
        """Govde gercekten bitti mi (baglanti yeniden kullanilabilir mi)?"""
        resp = self._resp
        if resp is None or not resp.isclosed():
            return False
        if getattr(resp, "will_close", True):
            return False               # sunucu kapatti / HTTP/1.0
        if getattr(resp, "version", 0) != 11:
            return False
        # Content-Length tam tuketilmediyse akis kesilmistir: soket guvenilmez
        return not getattr(resp, "length", None)

    def _recycle(self) -> None:
        self._done = True
        conn, self._conn = self._conn, None
        if not _conn_open(conn):
            _safe_close(conn)
            return
        _remember_session(conn, self._key)     # TLS 1.3 bileti burada gelir
        _pool().put(self._key, conn)

    def _drop(self) -> None:
        self._done = True
        conn, self._conn = self._conn, None
        _discard(conn)


def _new_conn(scheme: str, host: str, port: int, timeout: float,
              tlsig: tuple | None, key: tuple):
    """Bos bir baglanti nesnesi (henuz soket yok); TLS'te oturum sunulur."""
    if scheme == "https":
        settings, signature = tlsig
        sess = _pool().session(key)
        return PoolTLSConnection(host, port, timeout=timeout,
                                 context=_context_for(settings, signature),
                                 session=sess)
    return FastHTTPConnection(host, port, timeout=timeout)


def _remember_session(conn, key: tuple) -> None:
    """Baglantinin TLS oturumunu anahtara yazar.

    TLS 1.3 bileti el sikismasindan SONRA geldigi icin yanit okunduktan
    sonra tazelenir; TLS 1.2'de oturum el sikismasiyla hazirdir."""
    if not hasattr(conn, "tls_session"):
        return
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            conn.tls_session = sock.session or conn.tls_session
        except (ssl.SSLError, ValueError):
            pass
    _pool().store_session(key, conn.tls_session)


def _request_once(ep: tuple, url: str, opt: DlOptions, headers: dict,
                  timeout: float):
    """Tek GET: havuzdan baglanti al, gonder, yaniti sar.

    Bosta bulunan soket sunucu tarafinda kapanmissa istek TAZE baglantiyla
    bir kez yinelenir: GET tekrarlanabilir, kullanici bunu gormez."""
    scheme, host, port, target = ep
    tlsig = None
    if scheme == "https":
        settings = tls_settings(opt)
        tlsig = (settings, settings.fingerprint())
    key = (scheme, host, port, tlsig)
    pool = _pool()
    for attempt in (0, 1):
        conn = pool.take(key)
        reused = conn is not None
        try:
            if conn is None:
                conn = _new_conn(scheme, host, port, timeout, tlsig, key)
            else:
                # ayni soket farkli stall butcesiyle kullanilabilir
                conn.timeout = timeout
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
            conn.request("GET", target, headers=_send_headers(headers))
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as ex:
            _safe_close(conn)
            if reused and attempt == 0 and _stale(ex):
                continue
            if isinstance(ex, http.client.HTTPException):
                raise
            raise urllib.error.URLError(ex) from ex
        if tlsig is not None:
            _remember_session(conn, key)
        return PooledResponse(url, resp, key, conn)
    raise AssertionError("erisilemez")     # pragma: no cover


def _http_error(url: str, resp: PooledResponse,
                msg: str | None = None) -> urllib.error.HTTPError:
    """Yaniti HTTPError'a sarar (cagiran `close()` edebilir)."""
    return urllib.error.HTTPError(url, resp.status, msg or resp.reason,
                                  resp.headers, resp)


def _drain(resp: PooledResponse) -> None:
    """Kucuk 30x govdesini okuyup baglantiyi havuza kazandirir."""
    try:
        cl = resp.headers.get("Content-Length")
        if cl is not None and cl.strip().isdigit() and int(cl) <= 65536:
            resp.read(int(cl))
    except Exception:
        pass


def _pooled_get(url: str, opt: DlOptions, headers: dict, timeout: float):
    """Dogrudan GET: baglanti havuzu + yonlendirme zinciri.

    Yonlendirme `ScopedRedirectHandler` kuralini izler: hedef koken
    farkliysa kimlik basliklari atilir, Range/If-Range/Referer kalir (bkz.
    `strip_credentials`). Ayni hedefe MAX_REPEATS'ten fazla gidilirse
    dongu sayilir. Havuzun kapsamadigi bir hedefe (ASCII disi yol vb.)
    dusulurse zincirin kalani eski opener'da biter."""
    origin = origin_of(url)
    hdrs = dict(headers)
    counts: dict = {}
    hop = 0
    while True:
        ep = _endpoint(url)
        if ep is None:
            return _open_urllib(url, opt, hdrs, timeout)
        resp = _request_once(ep, url, opt, hdrs, timeout)
        if resp.status not in _REDIRECT_CODES:
            if 200 <= resp.status < 300:
                return resp
            raise _http_error(url, resp)
        loc = (resp.headers.get("Location") or "").strip()
        newurl = urljoin(url, loc) if loc else ""
        counts[newurl] = counts.get(newurl, 0) + 1
        _drain(resp)
        resp.close()
        if not newurl or hop >= MAX_REDIRECTS or counts[newurl] > MAX_REPEATS:
            raise _http_error(url, resp, "yonlendirme izlenemedi")
        hop += 1
        if not same_origin(origin, newurl):
            hdrs = strip_credentials(hdrs)
        url = newurl


def _req_headers(req) -> dict:
    """Request basliklari: urllib'in do_open'i gibi unredirected + normal."""
    hdrs = dict(getattr(req, "unredirected_hdrs", {}) or {})
    hdrs.update(req.headers)
    return hdrs


class _Opener:
    """h3 farkinda opener: urllib opener'i gibi `.open` sunar.

    h3 seciliyse istek h3 uzerinden gider; dogrudan (proxy'siz) istekler
    baglanti havuzundan (`_pooled_get`), kalanlar urllib opener'inden.
    Metin/segment cekimi (hls.py, dash.py, segments.py) bu imzayi
    kullandigi icin yan dosyalar ve HLS/DASH segmentleri de ayni yoldan
    gecer. `.handlers` urllib opener'indan gelir."""

    def __init__(self, opener: urllib.request.OpenerDirector, opt: DlOptions):
        self._opener = opener
        self._opt = opt

    @property
    def handlers(self):
        return self._opener.handlers

    def open(self, req, timeout: float | None = None, **kw):
        to = timeout or self._opt.timeout
        url = req.full_url
        if h3.use_h3(url, self._opt):
            return _open_any(url, self._opt, _req_headers(req), to)
        if not kw and req.get_method() == "GET" and _poolable(url, self._opt):
            return _pooled_get(url, self._opt, _req_headers(req), to)
        return self._opener.open(req, timeout=to, **kw)


class RangeInvalid(Exception):
    """Sunucu kaynagi degistirdi (If-Range 200 dondu) -> bastan indirilmeli."""


@dataclass
class DlOptions:
    user_agent: str = ""
    headers: dict = field(default_factory=dict)
    cookies: str = ""
    user: str = ""
    password: str = ""
    proxy: str = ""
    insecure: bool = False
    timeout: int = 30
    retries: int = 5
    # HTTP/3 (QUIC) tasimasi: "off" (stdlib varsayilani) | "auto" | "force".
    # "auto" yalnizca kaynak Alt-Svc ile h3 duyurduysa dener; aioquic
    # kurulu degilse/proxy varsa sessizce TCP'de kalir.
    http3: str = "off"
    # Kimlik kapsami: (sema, ana makine, port). Bu kokenin disindaki bir
    # adrese giden istekte kimlik basliklari (Authorization/Cookie/ozel
    # basliklar) gonderilmez: aynalar ve yonlendirme hedefleri parolayi
    # toplayamaz. Bos ise istek URL'inin kendi kokeni gecerlidir (tek
    # kaynak; ad-hoc kullanim) - yonlendirme kurali yine uygulanir.
    origin: tuple = ()
    # TLS ayarlari: HTTP, h3 ve FTPS ayni nesneyi paylasir. `tls_settings`
    # `insecure` alanini da birlestirir (eski cagri bicimi korunur).
    tls: TlsConfig = field(default_factory=TlsConfig)

    def base_headers(self) -> dict:
        h = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",  # Range dogrulugu icin
            # havuz yolu bunu keep-alive yapar (bkz. _send_headers)
            "Connection": "close",
        }
        h.update({k: v for k, v in self.headers.items()})
        if self.cookies:
            h["Cookie"] = self.cookies
        if self.user:
            # kimlik YALNIZ bu baslikla gonderilir: urllib'in parola deposu
            # (HTTPBasicAuthHandler) hic kurulmaz - kurulsaydi bilinmeyen
            # bir kokenin 401'ine de kimlikle karsilik verirdi
            tok = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            h["Authorization"] = "Basic " + tok
        return h

    def headers_for(self, url: str) -> dict:
        """`url`e gonderilecek basliklar.

        `origin` doluysa ve URL baska bir koken/adrese gidiyorsa kimlik
        basliklari DUSURULUR (ayna baska makinede: parola sizmaz; ayni
        kokenli ayna kimligi korur). Bak: api._item_params metalink
        kurali - ayni mantik."""
        h = self.base_headers()
        if not self.origin or same_origin(self.origin, url):
            return h
        return strip_credentials(h)


@dataclass
class ProbeResult:
    status: int = 0
    total: int = -1              # bilinmiyorsa -1
    accept_ranges: bool = False
    etag: str = ""
    last_modified: str = ""
    content_type: str = ""
    filename: str = ""
    final_url: str = ""
    content_disposition: str = ""


def _make_opener(opt: DlOptions, direct: bool = False):
    """Opener kurar; h3 seciliyse `open` istekleri h3'e devreder.

    direct=True: h3 yonlendirmesi kapali (h3'un TCP'ye dusus yolu, sonsuz
    donguye girmesin) ve dogrudan urllib opener'i doner. TLS baglami
    HTTPS istegi acilinca kurulur (bozuk TLS ayari duz http isini etkilemez)."""
    handlers: list[urllib.request.BaseHandler] = []
    if opt.proxy and socks.is_socks(opt.proxy):
        # SOCKS5: gecit HTTP degil, tunel acar; ProxyHandler kullanilmaz.
        handlers.append(SocksHTTPHandler(opt.proxy))
        handlers.append(SocksHTTPSHandler(opt.proxy, opt))
    else:
        if opt.proxy:
            handlers.append(urllib.request.ProxyHandler(
                {"http": opt.proxy, "https": opt.proxy}))
        else:
            # cevre degiskenlerindeki proxy'yi koru (urllib varsayilani)
            handlers.append(urllib.request.ProxyHandler())
        handlers.append(FastHTTPSHandler(opt))
        handlers.append(FastHTTPHandler())
    # yonlendirme kimlik kapsami: SOCKS/duz yol ayrimi yok, her opener'da
    # stdlib HTTPRedirectHandler'in yerine gecer (build_opener alt sinifi
    # gorunce varsayilani atlar)
    handlers.append(ScopedRedirectHandler())
    opener = urllib.request.build_opener(*handlers)
    return opener if direct else _Opener(opener, opt)


def _open_urllib(url: str, opt: DlOptions, headers: dict, timeout: float):
    """Eski yol: urllib opener'i (proxy/socks/havuz kapsami disi istekler)."""
    opener = _make_opener(opt, direct=True)
    return opener.open(urllib.request.Request(url, headers=headers),
                       timeout=timeout)


def _open_tcp(url: str, opt: DlOptions, headers: dict, timeout: float):
    """stdlib (TCP/TLS) yolu: tek istek, havuzdan yeniden kullanimli yanit.

    Gecitsiz dogrudan istekler baglanti havuzundan gecer (`_pooled_get`);
    proxy/SOCKS istekleri ve havuzun kapsamadigi adresler eski urllib
    opener'inda kalir."""
    if _poolable(url, opt):
        return _pooled_get(url, opt, headers, timeout)
    return _open_urllib(url, opt, headers, timeout)


def _open_any(url: str, opt: DlOptions, headers: dict, timeout: float):
    """h3 seciliyse HTTP/3, yoksa TCP. auto modda h3 hatasi TCP'ye duser.

    HTTPError (sunucu yanit verdi) ve force modda H3Unavailable yukari
    cikar; tasima hatasinda h3 kaynagi zaten "bozuk" isaretler.
    """
    if h3.use_h3(url, opt):
        try:
            return h3.request(url, opt, headers, timeout)
        except (urllib.error.HTTPError, h3.H3Unavailable):
            raise
        except Exception:
            if h3.mode_of(opt) == "force":
                raise
    return _open_tcp(url, opt, headers, timeout)


def response_socket(resp):
    """Yanitin altindaki TCP soketi (yoksa None).

    TCP_INFO orneklemesi icin: urllib/http.client yanitinda `fp.raw._sock`
    gercek sokettir; h3 (QUIC) yanitinda ve sahte yanitlarda TCP soketi
    yoktur, None doner. Soket bulunamazsa indirme yolu etkilenmez.
    """
    if resp is None:
        return None
    fp = getattr(resp, "fp", None)
    raw = getattr(fp, "raw", None)
    for cand in (getattr(raw, "_sock", None), raw,
                 getattr(resp, "_sock", None), getattr(resp, "sock", None)):
        if cand is not None and callable(getattr(cand, "getsockopt", None)):
            return cand
    return None


def _probe_from(res) -> ProbeResult:
    """Yanit nesnesinden (urllib ya da h3) kaynak ozellikleri."""
    res_probe = ProbeResult()
    res_probe.status = res.status
    res_probe.final_url = res.geturl()
    res_probe.content_type = res.headers.get("Content-Type", "") or ""
    cr = res.headers.get("Content-Range", "") or ""
    if res.status == 206 and "/" in cr:
        total = cr.rsplit("/", 1)[1].strip()
        if total.isdigit():
            res_probe.total = int(total)
        res_probe.accept_ranges = True
    elif res.headers.get("Accept-Ranges", "").lower() == "bytes":
        res_probe.accept_ranges = True
        cl = res.headers.get("Content-Length", "")
        if cl.isdigit():
            res_probe.total = int(cl)
    else:
        cl = res.headers.get("Content-Length", "")
        if cl.isdigit():
            res_probe.total = int(cl)
    res_probe.etag = res.headers.get("ETag", "") or ""
    res_probe.last_modified = res.headers.get("Last-Modified", "") or ""
    res_probe.filename = res.headers.get_filename() or ""
    res_probe.content_disposition = res.headers.get(
        "Content-Disposition", "") or ""
    # 200 donduysa toplam boyut genelde Content-Length'tir
    if res_probe.total < 0:
        cl = res.headers.get("Content-Length", "")
        res_probe.total = int(cl) if cl.isdigit() else -1
    return res_probe


# Range yoklamasini reddeden durumlar: sunucu bytes=0-0 istegine ozgu
# hata donduruyor olabilir. Tek duz GET ile yeniden denenir; basariliysa
# kaynak Range'siz tek akis sayilir. 404/401 gibi gercek hatalar burada
# yok: onlar yoklamanin kendisi kadar dosyanin da sorunu.
_PROBE_RANGE_REJECT = frozenset({400, 403, 405, 416, 501})


def probe(url: str, opt: DlOptions) -> ProbeResult:
    """Range: bytes=0-0 ile kaynak ozelliklerini kesfeder.

    Range yoklamasi reddedilirse (400/403/405/416/501) bir kez duz GET
    denenir: yalniz basliklar okunur, govde kapatilir. Bu durumda kaynak
    Range desteklemiyormus gibi tek akis kabul edilir (Accept-Ranges
    basligi olsa bile: sunucu fiilen bytes=0-0 istegini geri cevirdi)."""
    headers = {**opt.headers_for(url), "Range": "bytes=0-0"}
    fallback = False
    try:
        res = _open_any(url, opt, headers, opt.timeout)
    except urllib.error.HTTPError as e:
        try:
            e.close()
        except Exception:
            pass
        if e.code not in _PROBE_RANGE_REJECT:
            raise
        # duz GET: Range basligi yok, yalniz basliklar ilgilendirir
        try:
            res = _open_any(url, opt, opt.headers_for(url), opt.timeout)
        except urllib.error.HTTPError as plain:
            try:
                plain.close()
            except Exception:
                pass
            raise
        fallback = True
    try:
        out = _probe_from(res)
        if fallback:
            out.accept_ranges = False
        # Alt-Svc ogrenimi: yalniz yanitin SON adresi icin (yonlendirme
        # hedefi baska bir kaynagin h3 ayarini belirleyemez) ve isin
        # guvenilirlik sinifiyla ayni anahtara yazilir.
        final = out.final_url or res.geturl() or url
        if final:
            h3.remember_alt_svc(final, res.headers.get("Alt-Svc"),
                                bool(opt.insecure))
    finally:
        res.close()
    return out


def open_range(url: str, opt: DlOptions, start: int, end: int,
               etag: str = "", last_modified: str = "",
               timeout: int | None = None):
    """Tek Range baglantisi acar. 200 donerse RangeInvalid firlatir.

    timeout hem baglanti kurulumuna hem her okumaya uygulanir; veri
    akisi bu sure boyunca durursa okuma TimeoutError ile kesilir (stall).
    """
    to = timeout or opt.timeout
    h = opt.headers_for(url)
    h["Range"] = f"bytes={start}-{end}"
    if etag or last_modified:
        h["If-Range"] = etag or last_modified
    try:
        resp = _open_any(url, opt, h, to)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            raise RangeInvalid("416") from e
        raise
    if resp.status == 200 and start > 0:
        resp.close()
        raise RangeInvalid("sunucu Range'i yok saydi (200)")
    return resp


def request_of(url: str, opt: DlOptions) -> urllib.request.Request:
    """Tek istek nesnesi. Basliklar koken kapsamina gore suzulur
    (`headers_for`): ayna/yan dosya baska koken ise kimlik gitmez.

    Yalniz http(s) kabul edilir: bu fabrika yan dosya/manifest/segment
    istekleri icindir; ftp/file gibi semalar urllib opener'ina sizmamali
    (FTP isleri ftp.py uzerinden gider)."""
    scheme = urlsplit(str(url or "")).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(
            f"desteklenmeyen tasima semasi: {scheme or 'yok'}")
    req = urllib.request.Request(url, headers=opt.headers_for(url))
    # yonlendirme zinciri boyunca ILK koken korunur (ScopedRedirectHandler)
    req._ferdim_origin = origin_of(url)
    return req


def fetch_small(url: str, opt: DlOptions, timeout: float = 8.0,
                limit: int = 65536, allow_html: bool = False) -> str:
    """Kucuk bir metin kaynagi ceker (saglama yan dosyasi vb.). HTML sayfa
    (yumusak 404) ya da limitin uzerinde govde reddedilir; allow_html yalniz
    yanlis Content-Type ile servis edilen belgeler icin.

    h3 acikken ayni `_open_any` seciminden gecer (yan dosyalar/manifestler
    boylece Range akisiyla ayni baglantiyi paylasir)."""
    res = _open_any(url, opt, opt.headers_for(url), timeout)
    try:
        if not allow_html and \
                "html" in (res.headers.get("Content-Type") or "").lower():
            raise ValueError("html yanit")
        data = res.read(limit + 1)
        if res.headers.get("Alt-Svc"):
            final = res.geturl() or url
            h3.remember_alt_svc(final, res.headers.get("Alt-Svc"),
                                bool(opt.insecure))
    finally:
        res.close()
    if len(data) > limit:
        raise ValueError("govde cok buyuk")
    return data.decode("utf-8", "replace")


def is_hls(url: str, content_type: str) -> bool:
    u = url.lower().split("?", 1)[0]
    return u.endswith((".m3u8", ".m3u")) or "mpegurl" in (content_type or "").lower()
