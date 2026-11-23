"""urllib tabanli HTTP yardimcilari: probe, Range akisi, opener kurulumu.

Turbo modu icin baglantilara buyuk SO_RCVBUF uygulanir: cekirdek her
baglanti icin daha derin bir alma kuyrugu tutar, hat daha hizli dolar.
"""
from __future__ import annotations

import base64
import http.client
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field

RCVBUF = 1 << 21  # 2 MB (cekirdek rmem_max ile kisitlanir, sorun degil)


class FastHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
        except OSError:
            pass


class FastHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
        except OSError:
            pass


class FastHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(FastHTTPConnection, req)


class FastHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(FastHTTPSConnection, req)


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
            tok = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            h["Authorization"] = "Basic " + tok
        return h


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


def _make_opener(opt: DlOptions):
    handlers: list[urllib.request.BaseHandler] = []
    if opt.proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": opt.proxy, "https": opt.proxy}))
    else:
        # cevre degiskenlerindeki proxy'yi koru (urllib varsayilani)
        handlers.append(urllib.request.ProxyHandler())
    if opt.insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(FastHTTPSHandler(context=ctx))
    else:
        handlers.append(FastHTTPSHandler())
    handlers.append(FastHTTPHandler())
    return urllib.request.build_opener(*handlers)


def probe(url: str, opt: DlOptions) -> ProbeResult:
    """Range: bytes=0-0 ile kaynak ozelliklerini kesfeder."""
    res = ProbeResult()
    opener = _make_opener(opt)
    req = urllib.request.Request(url, headers={**opt.base_headers(),
                                               "Range": "bytes=0-0"})
    with opener.open(req, timeout=opt.timeout) as r:
        res.status = r.status
        res.final_url = r.geturl()
        res.content_type = r.headers.get("Content-Type", "") or ""
        cr = r.headers.get("Content-Range", "") or ""
        if r.status == 206 and "/" in cr:
            total = cr.rsplit("/", 1)[1].strip()
            if total.isdigit():
                res.total = int(total)
            res.accept_ranges = True
        elif r.headers.get("Accept-Ranges", "").lower() == "bytes":
            res.accept_ranges = True
            cl = r.headers.get("Content-Length", "")
            if cl.isdigit():
                res.total = int(cl)
        else:
            cl = r.headers.get("Content-Length", "")
            if cl.isdigit():
                res.total = int(cl)
        res.etag = r.headers.get("ETag", "") or ""
        res.last_modified = r.headers.get("Last-Modified", "") or ""
        res.filename = r.headers.get_filename() or ""
        res.content_disposition = r.headers.get("Content-Disposition", "") or ""
        # 200 donduysa toplam boyut genelde Content-Length'tir
        if res.total < 0:
            cl = r.headers.get("Content-Length", "")
            res.total = int(cl) if cl.isdigit() else -1
    return res


def open_range(url: str, opt: DlOptions, start: int, end: int,
               etag: str = "", last_modified: str = "",
               timeout: int | None = None):
    """Tek Range baglantisi acar. 200 donerse RangeInvalid firlatir.

    timeout hem baglanti kurulumuna hem her okumaya uygulanir; veri
    akisi bu sure boyunca durursa okuma TimeoutError ile kesilir (stall).
    """
    opener = _make_opener(opt)
    h = opt.base_headers()
    h["Range"] = f"bytes={start}-{end}"
    if etag or last_modified:
        h["If-Range"] = etag or last_modified
    req = urllib.request.Request(url, headers=h)
    try:
        resp = opener.open(req, timeout=timeout or opt.timeout)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            raise RangeInvalid("416") from e
        raise
    if resp.status == 200 and start > 0:
        resp.close()
        raise RangeInvalid("sunucu Range'i yok saydi (200)")
    return resp


def request_of(url: str, opt: DlOptions) -> urllib.request.Request:
    return urllib.request.Request(url, headers=opt.base_headers())


def fetch_small(url: str, opt: DlOptions, timeout: float = 8.0,
                limit: int = 65536, allow_html: bool = False) -> str:
    """Kucuk bir metin kaynagi ceker (saglama yan dosyasi vb.). HTML sayfa
    (yumusak 404) ya da limitin uzerinde govde reddedilir; allow_html yalniz
    yanlis Content-Type ile servis edilen belgeler icin."""
    opener = _make_opener(opt)
    with opener.open(request_of(url, opt), timeout=timeout) as r:
        if not allow_html and \
                "html" in (r.headers.get("Content-Type") or "").lower():
            raise ValueError("html yanit")
        data = r.read(limit + 1)
        if len(data) > limit:
            raise ValueError("govde cok buyuk")
        return data.decode("utf-8", "replace")


def is_hls(url: str, content_type: str) -> bool:
    u = url.lower().split("?", 1)[0]
    return u.endswith((".m3u8", ".m3u")) or "mpegurl" in (content_type or "").lower()
