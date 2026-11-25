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
"""
from __future__ import annotations

import base64
import functools
import http.client
import os
import socket
import ssl
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

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


class _Opener:
    """h3 farkinda opener: urllib opener'i gibi `.open` sunar.

    h3 seciliyse istek h3 uzerinden gider; degilse urllib opener'i aynen
    calisir. Metin/segment cekimi (hls.py, dash.py, segments.py) bu imzayi
    kullandigi icin yan dosyalar ve HLS/DASH segmentleri de h3'ten gecer.
    `.handlers` urllib opener'indan gelir."""

    def __init__(self, opener: urllib.request.OpenerDirector, opt: DlOptions):
        self._opener = opener
        self._opt = opt

    @property
    def handlers(self):
        return self._opener.handlers

    def open(self, req, timeout: float | None = None, **kw):
        to = timeout or self._opt.timeout
        if h3.use_h3(req.full_url, self._opt):
            return _open_any(req.full_url, self._opt, dict(req.headers), to)
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


def _open_tcp(url: str, opt: DlOptions, headers: dict, timeout: float):
    """stdlib (TCP/TLS) yolu: tek istek, urllib yaniti."""
    opener = _make_opener(opt, direct=True)
    return opener.open(urllib.request.Request(url, headers=headers),
                       timeout=timeout)


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
