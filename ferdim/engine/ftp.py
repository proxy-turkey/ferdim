"""FTP / FTPS indirme (stdlib ftplib).

Tek akis: sunucu boyutu SIZE ile ogrenilir; yarim kalan dosya .part
boyutundan REST ile surdurulur. ftps:// AUTH TLS (explicit) + prot_p
kullanir; sertifika dogrulamasi config'teki verify_tls'e uyar.
"""
from __future__ import annotations

import errno
import ftplib
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass, replace
from urllib.parse import unquote, urlsplit

from .. import util
from . import httpcli, socks

BLOCK = 1 << 16          # 64 KB veri blogu
POLL = 2.0               # veri okuma yoklamasi: durdurma en gec bu kadar bekler
_FD_ERRNOS = frozenset({errno.EMFILE, errno.ENFILE})

_FTP_WORDS = {
    421: "servis su an kullanilamiyor",
    425: "veri baglantisi kurulamadi",
    426: "aktarim kesildi",
    430: "kimlik dogrulama gerekli",
    530: "kimlik dogrulama gerekli",
    532: "hesap gerekli",
    550: "kaynak bulunamadi",
    551: "kaynak kullanilamiyor",
    552: "depolama alani yetersiz",
    553: "gecersiz dosya adi",
}


class FtpError(Exception):
    """Kullaniciya gosterilecek FTP hatasi (ASCII-Turkce). code: API
    hata kodu (job._code_for okur); transient: yeniden denenebilir mi."""

    def __init__(self, msg: str, code: str = "unknown",
                 transient: bool = False):
        super().__init__(msg)
        self.code = code
        self.transient = transient


class RestUnsupported(Exception):
    """Sunucu REST ile devam etmeyi desteklemiyor (bastan indirilir)."""


@dataclass(frozen=True)
class _Target:
    secure: bool
    host: str
    port: int
    user: str
    password: str
    path: str


def is_ftp(url: str) -> bool:
    try:
        return urlsplit(url).scheme.lower() in ("ftp", "ftps")
    except ValueError:
        return False


def parse_url(url: str, user: str = "", password: str = "") -> _Target:
    """URL'yi baglanti bilgisine cevirir. Kimlik bilgisi URL'de yoksa
    verilen yedek (spec.user/password), o da yoksa anonim giris kullanilir;
    yuzde kodlamasi cozulur."""
    try:
        parts = urlsplit(url)
        port = parts.port or 21
    except ValueError:
        raise FtpError("gecersiz FTP adresi") from None
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if scheme not in ("ftp", "ftps") or not host:
        raise FtpError("gecersiz FTP adresi")
    raw = unquote(parts.path) or "/"
    # RFC 1738 ';type=i' eki sunucuya yolun parcasi olarak gitmemeli
    raw = re.sub(r";type=[aid]$", "", raw, flags=re.I)
    path = raw if raw.startswith("/") else "/" + raw
    if "\r" in path or "\n" in path:
        raise FtpError("gecersiz kaynak yolu")
    if parts.username:
        uname = unquote(parts.username)
        pword = unquote(parts.password) if parts.password is not None else ""
    elif user:
        uname, pword = user, password or ""
    else:
        uname, pword = "anonymous", "anonymous@"
    return _Target(scheme == "ftps", host, port, uname, pword, path)


def _code(ex: BaseException) -> int:
    try:
        return int(str(ex)[:3])
    except (TypeError, ValueError):
        return 0


_API_CODES = {430: "auth", 530: "auth", 532: "auth"}


def _perm_code(text: str) -> str:
    """550/551 ayni kod degil: metin yoklugu mu, izin reddi mi?"""
    low = text.lower()
    if any(s in low for s in ("no such file", "not found", "does not exist")):
        return "not_found"
    if any(s in low for s in ("permission", "denied", "access")):
        return "forbidden"
    return "unknown"


def _perm_error(ex: ftplib.error_perm) -> FtpError:
    code = _code(ex)
    kind = _perm_code(str(ex)) if code in (550, 551) else "unknown"
    if kind == "not_found":
        msg = f"kaynak bulunamadi (FTP {code})"
    elif kind == "forbidden":
        msg = f"erisim engellendi (FTP {code})"
    else:
        name = _FTP_WORDS.get(code)
        msg = f"{name} (FTP {code})" if name else f"FTP hatasi (FTP {code})"
    return FtpError(msg, _API_CODES.get(code, kind))


def _wrap(ex: BaseException, connecting: bool = False) -> FtpError:
    if isinstance(ex, FtpError):
        return ex
    if isinstance(ex, httpcli.TlsConfigError):
        return FtpError(str(ex), "tls_config")
    if isinstance(ex, socks.SocksError):
        return FtpError(str(ex), ex.code, transient=ex.code == "network")
    if isinstance(ex, socket.gaierror):
        return FtpError("sunucu adi cozulemedi", "network", transient=True)
    if isinstance(ex, ConnectionRefusedError):
        return FtpError("sunucuya baglanilamadi (baglanti reddedildi)",
                        "network", transient=True)
    if isinstance(ex, (TimeoutError, socket.timeout)):
        return FtpError("baglanti zaman asimi", "network", transient=True)
    if isinstance(ex, OSError) and ex.errno in _FD_ERRNOS:
        # fd tukendi (EMFILE/ENFILE): yeniden denemek ise yaramaz, job
        # katmani bu kodu kalici altyapi hatasi sayar (bkz. job fd_exhausted)
        return FtpError("dosya taniticisi tukendi", "fd_exhausted")
    if isinstance(ex, EOFError):
        return FtpError("kontrol baglantisi kapandi", "network",
                        transient=True)
    if isinstance(ex, ssl.SSLError):
        why = getattr(ex, "verify_message", None) or getattr(ex, "reason", None) \
            or str(ex)
        return FtpError("guvenli baglanti kurulamadi: " + str(why), "network",
                        transient=True)
    if isinstance(ex, ftplib.error_perm):
        return _perm_error(ex)
    if isinstance(ex, ftplib.error_temp):
        code = _code(ex)
        name = _FTP_WORDS.get(code)
        return FtpError(f"{name} (FTP {code})" if name
                        else f"FTP gecici hatasi (FTP {code})",
                        "network", transient=True)
    if isinstance(ex, ftplib.error_reply):
        code = _code(ex)
        name = _FTP_WORDS.get(code)
        return FtpError(f"{name} (FTP {code})" if name
                        else f"beklenmeyen FTP yaniti (FTP {code})",
                        "network" if code < 500 else "unknown",
                        transient=code < 500)
    if isinstance(ex, ftplib.error_proto):
        return FtpError("sunucu yaniti anlasilamadi", "network",
                        transient=True)
    if isinstance(ex, ftplib.Error):
        return FtpError("FTP hatasi: " + (str(ex) or type(ex).__name__))
    if isinstance(ex, ConnectionError):
        return FtpError("baglanti kesildi: "
                        + (getattr(ex, "strerror", None) or str(ex)),
                        "network", transient=True)
    if isinstance(ex, OSError):
        if ex.errno in util.DISK_ERRNOS:
            return FtpError("girdi/cikti hatasi: "
                            + (getattr(ex, "strerror", None) or str(ex)),
                            "disk")
        lead = "sunucuya baglanilamadi: " if connecting else "girdi/cikti hatasi: "
        return FtpError(lead + (getattr(ex, "strerror", None) or str(ex)),
                        "network" if connecting else "unknown",
                        transient=True)
    return FtpError(str(ex) or type(ex).__name__)


class _SocksFTP:
    """ftplib baglantilarini SOCKS5 tunelinden gecirir.

    Kontrol ve pasif veri baglantisi ayni tunelden acilir (PASV'in verdigi
    ozel adres gecitten gorunmeyebilir; veri baglantisi kontrol sunucusu
    adresine kurulur). Aktif mod SOCKS uzerinden kurulamaz: gecit geri
    baglanti kabul etmez, bu yuzden pasif mod zorunludur.
    """

    _socks_proxy = ""            # alt siniflar ornekte doldurur

    def connect(self, host: str = "", port: int = 0, timeout=-999,
                source_address=None):
        # ftplib.FTP.connect ile ayni sozlesme; tek fark soketin tunelden
        # gelmesi (stdlib kodu kopyalanir, cunku create_connection'e
        # mudahale edilemiyor).
        if host != "":
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if source_address is not None:
            self.source_address = source_address
        if self.timeout is not None and not self.timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        self.sock = socks.open_tunnel(self._socks_proxy, self.host, self.port,
                                      self.timeout)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd: str, rest=None):
        """Pasif veri baglantisini tunelden acar (gerekirse TLS ile sarar)."""
        if not self.passiveserver:
            raise socks.SocksError("FTP SOCKS yalniz pasif modda calisir",
                                   "incompatible")
        _host, port = self.makepasv()
        conn = socks.open_tunnel(self._socks_proxy, self.host, port,
                                 self.timeout)
        try:
            if rest is not None:
                self.sendcmd("REST %s" % rest)
            resp = self.sendcmd(cmd)
            # bazi sunucular LIST/STOR icin once 200 doner (protokol disi)
            if resp[0] == "2":
                resp = self.getresp()
            if resp[0] != "1":
                raise ftplib.error_reply(resp)
        except BaseException:
            conn.close()
            raise
        size = ftplib.parse150(resp) if resp[:3] == "150" else None
        if getattr(self, "_prot_p", False):
            conn = self.context.wrap_socket(conn, server_hostname=self.host)
        return conn, size


class _SocksFTPPlain(_SocksFTP, ftplib.FTP):
    def __init__(self, proxy: str, *args, **kw):
        super().__init__(*args, **kw)
        self._socks_proxy = proxy


class _SocksFTPTLS(_SocksFTP, ftplib.FTP_TLS):
    def __init__(self, proxy: str, *args, **kw):
        super().__init__(*args, **kw)
        self._socks_proxy = proxy


def _ssl_context(tls, insecure: bool) -> ssl.SSLContext:
    """TLS baglami: HTTP/h3 ile ayni kaynaktan (httpcli) gelir."""
    settings = httpcli.tls_settings(tls) if tls is not None \
        else httpcli.TlsConfig()
    if insecure and not settings.insecure:
        settings = replace(settings, insecure=True)
    return httpcli.ssl_context(settings)


def _connect(t: _Target, *, timeout: float, insecure: bool,
             stall: float, proxy: str = "", tls=None) -> ftplib.FTP:
    # FTP yalniz SOCKS5 tunelini kullanir; HTTP gecidi FTP'ye uygulanmaz.
    use_socks = bool(proxy) and socks.is_socks(proxy)
    if t.secure:
        ctx = _ssl_context(tls, insecure)
        if use_socks:
            ftp: ftplib.FTP = _SocksFTPTLS(proxy, context=ctx, timeout=timeout)
        else:
            ftp = ftplib.FTP_TLS(context=ctx, timeout=timeout)
    else:
        ftp = _SocksFTPPlain(proxy, timeout=timeout) if use_socks \
            else ftplib.FTP(timeout=timeout)
    try:
        ftp.connect(t.host, t.port)
        ftp.login(t.user, t.password)
        if t.secure:
            ftp.prot_p()              # veri kanali da sifreli
        ftp.set_pasv(True)
        ftp.voidcmd("TYPE I")
        if ftp.sock is not None:
            ftp.sock.settimeout(max(float(stall), 1.0))
        return ftp
    except BaseException as ex:
        try:
            ftp.close()
        except Exception:
            pass
        raise _wrap(ex, connecting=True) from None


def _close(ftp: ftplib.FTP, graceful: bool) -> None:
    if graceful:
        try:
            ftp.quit()
            return
        except Exception:
            pass
    try:
        ftp.close()
    except Exception:
        pass


def probe_meta(url: str, *, timeout: int = 30, insecure: bool = False,
               stall: float | None = None, user: str = "", password: str = "",
               proxy: str = "", tls=None) -> tuple[int, str]:
    """(boyut, MDTM) dondurur. SIZE yoksa boyut -1, MDTM yoksa zaman bos.
    Yol yoksa FtpError."""
    t = parse_url(url, user, password)
    stall = float(stall or max(int(timeout), 5))
    try:
        ftp = _connect(t, timeout=timeout, insecure=insecure, stall=stall,
                       proxy=proxy, tls=tls)
    except BaseException as ex:
        raise _wrap(ex) from None
    try:
        try:
            size = ftp.size(t.path)
        except ftplib.error_perm as ex:
            if _code(ex) in (500, 502, 504):
                size = None               # SIZE desteklenmiyor
            else:
                raise
        mtime = ""
        try:
            resp = ftp.sendcmd("MDTM " + t.path)
            if resp[:3] == "213":
                mtime = resp[3:].strip()
        except (ftplib.error_perm, ftplib.error_temp):
            pass                           # MDTM istege bagli
        return (int(size) if size is not None else -1), mtime
    except BaseException as ex:
        raise _wrap(ex) from None
    finally:
        _close(ftp, graceful=True)


def probe_size(url: str, *, timeout: int = 30, insecure: bool = False,
               stall: float | None = None, user: str = "", password: str = "",
               proxy: str = "", tls=None) -> int:
    """Dosya boyutu; sunucu SIZE desteklemiyorsa -1. Yol yoksa FtpError."""
    return probe_meta(url, timeout=timeout, insecure=insecure, stall=stall,
                      user=user, password=password, proxy=proxy, tls=tls)[0]


def download(url: str, fd: int, offset: int, *, stop, bucket, progress,
             stalled, insecure: bool = False, timeout: int = 30,
             halted=None, user: str = "", password: str = "",
             proxy: str = "", tls=None) -> int:
    """offset'ten EOF'a indirir, yazilan bayt sayisini dondurur.

    stop   : threading.Event; bekleme ve okuma aralarinda gorulur.
    halted : ek durdurma kosulu (is kusagi degistiyse eski akis cekilir).
    stalled: bu kadar saniye bayt gelmezse TimeoutError sayilir.
    Durdurma gelirse eldeki blok yazilmaz; .part diskte kalir, devam eder.
    2xx disi son yanit basarili sayilmaz (yarim aktarim 'done' olmaz).
    """
    t = parse_url(url, user, password)
    stall = max(1.0, float(stalled))

    def aborted() -> bool:
        return stop.is_set() or (halted is not None and halted())

    if aborted():
        return 0
    try:
        ftp = _connect(t, timeout=timeout, insecure=insecure, stall=stall,
                       proxy=proxy, tls=tls)
    except BaseException as ex:
        raise _wrap(ex) from None
    written = 0
    done = False
    try:
        if aborted():
            return 0
        try:
            data = ftp.transfercmd(f"RETR {t.path}", rest=offset or None)
        except ftplib.error_perm as ex:
            if offset and _code(ex) in (500, 501, 502, 504):
                raise RestUnsupported() from None
            raise
        last = time.monotonic()
        data.settimeout(min(POLL, stall))
        try:
            while True:
                if aborted():
                    break
                try:
                    block = data.recv(BLOCK)
                except socket.timeout:
                    if time.monotonic() - last > stall:
                        raise FtpError(
                            "okuma hatasi: akis durdu (zaman asimi)",
                            "stall", transient=True) from None
                    continue
                if not block:
                    break
                last = time.monotonic()
                if aborted():
                    break
                if not bucket.take(len(block), stop) or aborted():
                    break
                os.pwrite(fd, block, offset + written)
                written += len(block)
                progress(len(block))
        finally:
            try:
                data.close()
            except Exception:
                pass
        # Veri akisi bitti: son yanit 2xx olmali. 426/4xx ya da baska
        # bir hata aktarimin yarim kaldigini gosterir; basarili sayilmaz
        # (SIZE yoksa bile toplam, dogrulanmis son yanittan sonra yazilir).
        completed = not aborted()
        if completed:
            ftp.voidresp()
        done = completed
    except RestUnsupported:
        raise
    except BaseException as ex:
        raise _wrap(ex) from None
    finally:
        _close(ftp, graceful=done)
    return written
