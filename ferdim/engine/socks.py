"""SOCKS5 istemcisi (RFC 1928) + kullanici/sifre dogrulamasi (RFC 1929).

Saf stdlib: motor dis bagimlilik kullanmaz. Bu modul yalnizca gecit
uzerinden TCP tuneli acar; TLS'i cagiran katman (httpcli) kurar.
`socks5://` hedef adi yerelde cozer, `socks5h://` adi gecide birakir
(kullanicinin DNS'i sizmaz).
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

DEFAULT_PORT = 1080
_SCHEMES = ("socks5", "socks5h")

# RFC 1928 REP kodlari
_REPLIES = {
    1: "genel hata",
    2: "kural baglantiya izin vermedi",
    3: "ag erisilemiyor",
    4: "hedef sunucuya erisilemiyor",
    5: "baglanti reddedildi",
    6: "TTL doldu",
    7: "komut desteklenmiyor",
    8: "adres turu desteklenmiyor",
}


class SocksError(OSError):
    """Gecit hatasi. Mesaj ASCII-Turkce kalir; code arayuz hata kodudur
    (auth, forbidden, network). Sifre asla mesaja girmez."""

    def __init__(self, msg: str, code: str = "network"):
        super().__init__(msg)
        self.code = code


@dataclass(frozen=True)
class Proxy:
    scheme: str
    host: str
    port: int
    user: str = ""
    password: str = ""

    @property
    def remote_dns(self) -> bool:
        return self.scheme == "socks5h"

    def __repr__(self) -> str:
        # dataclass repr'i sifreyi yazardi; hata ayiklama ciktisi sizmasin
        return (f"Proxy(scheme={self.scheme!r}, host={self.host!r}, "
                f"port={self.port!r}, user={self.user!r}, password=***)")


def is_socks(url: str) -> bool:
    """proxy degeri bir SOCKS5 adresi mi? (bozuk URL'ler False)."""
    try:
        return urlsplit(url).scheme.lower() in _SCHEMES
    except ValueError:
        return False


def parse(url: str) -> Proxy:
    """'socks5(h)://[kullanici:sifre@]host[:port]' -> Proxy."""
    try:
        parts = urlsplit(url)
        port = parts.port if parts.port is not None else DEFAULT_PORT
    except ValueError:
        raise SocksError("gecersiz SOCKS5 gecit adresi") from None
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if scheme not in _SCHEMES or not host or not 1 <= port <= 65535:
        raise SocksError("gecersiz SOCKS5 gecit adresi")
    user = unquote(parts.username) if parts.username else ""
    password = unquote(parts.password) if parts.password is not None else ""
    if len(user.encode("utf-8")) > 255 or len(password.encode("utf-8")) > 255:
        raise SocksError("gecersiz SOCKS5 gecit adresi (kimlik cok uzun)")
    return Proxy(scheme, host, port, user, password)


def _recv(sock: socket.socket, n: int) -> bytes:
    """Tam n bayt oku; erken EOF protokol hatasidir."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SocksError("SOCKS5 geciti baglantiyi kapatti")
        buf.extend(chunk)
    return bytes(buf)


def _auth(sock: socket.socket, p: Proxy) -> None:
    user = p.user.encode("utf-8")
    password = p.password.encode("utf-8")
    sock.sendall(b"\x01" + bytes([len(user)]) + user
                 + bytes([len(password)]) + password)
    ver, status = _recv(sock, 2)
    if ver != 1:
        raise SocksError("SOCKS5 kimlik dogrulama protokol hatasi")
    if status != 0:
        raise SocksError("SOCKS5 geciti kullanici/sifre kabul etmedi", "auth")


def _target(host: str, port: int, remote_dns: bool) -> bytes:
    """Hedefi ATYP + adres olarak kodlar (port ayrica eklenir)."""
    if remote_dns:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            try:
                name = host.encode("idna")
            except UnicodeError:
                raise SocksError("gecersiz hedef adi") from None
            if not name or len(name) > 255:
                raise SocksError("gecersiz hedef adi")
            return b"\x03" + bytes([len(name)]) + name
    else:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise SocksError("hedef sunucu adi cozulemedi") from None
        if not infos:
            raise SocksError("hedef sunucu adi cozulemedi")
        ip = ipaddress.ip_address(infos[0][4][0])
    return (b"\x01" + ip.packed) if ip.version == 4 \
        else (b"\x04" + ip.packed)


def open_tunnel(proxy: Proxy | str, host: str, port: int,
                timeout: float | None = None) -> socket.socket:
    """Gecit uzerinden host:port'a TCP tuneli acar; bagli soketi doner.

    Timeout kurulumun her adimina ve sonraki okumalara uygulanir (stall
    denetimi httpcli/ftp katmaninda devam eder).
    """
    p = proxy if isinstance(proxy, Proxy) else parse(proxy)
    try:
        sock = socket.create_connection((p.host, p.port), timeout)
    except OSError as ex:
        raise SocksError("SOCKS5 gecidine baglanilamadi: "
                         + (getattr(ex, "strerror", None) or str(ex))) from ex
    try:
        if isinstance(timeout, (int, float)) and timeout > 0:
            sock.settimeout(timeout)
        _handshake(sock, p, host, port)
        return sock
    except (TimeoutError, socket.timeout):
        sock.close()
        raise SocksError("SOCKS5 gecidi zaman asimi") from None
    except BaseException:
        sock.close()
        raise


def _handshake(sock: socket.socket, p: Proxy, host: str, port: int) -> None:
    methods = [0x00] + ([0x02] if p.user else [])
    sock.sendall(b"\x05" + bytes([len(methods)]) + bytes(methods))
    ver, method = _recv(sock, 2)
    if ver != 5:
        raise SocksError("SOCKS5 protokol hatasi (surum)")
    if method == 0x02:
        if not p.user:
            raise SocksError("SOCKS5 geciti kimlik dogrulama istiyor", "auth")
        _auth(sock, p)
    elif method == 0xFF:
        raise SocksError("SOCKS5 geciti kimlik dogrulamayi reddetti"
                         if p.user else
                         "SOCKS5 geciti kimlik dogrulama istiyor", "auth")
    elif method != 0x00:
        raise SocksError("SOCKS5 protokol hatasi (yontem)")
    # CONNECT + hedef + port
    sock.sendall(b"\x05\x01\x00" + _target(host, port, p.remote_dns)
                 + int(port).to_bytes(2, "big"))
    ver, rep, _rsv, atyp = _recv(sock, 4)
    if ver != 5:
        raise SocksError("SOCKS5 protokol hatasi (yanit)")
    # bagli adres + port: dogrulamak icin tumunu tuket
    if atyp == 0x01:
        _recv(sock, 4)
    elif atyp == 0x04:
        _recv(sock, 16)
    elif atyp == 0x03:
        _recv(sock, _recv(sock, 1)[0])
    else:
        raise SocksError("SOCKS5 protokol hatasi (adres turu)")
    _recv(sock, 2)
    if rep != 0:
        raise SocksError("SOCKS5 gecidi baglanti kurulamadi: "
                         + _REPLIES.get(rep, "bilinmeyen hata"),
                         "forbidden" if rep == 2 else "network")
