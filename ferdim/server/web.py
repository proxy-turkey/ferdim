"""127.0.0.1 HTTP API + yerel web arayuzu.

Guvenlik: baglantiyi acan surecin uid'i `/proc/net/tcp`ten dogrulanir; ayni
kullanici degilse her yol (/, /api/*, /metrics) 403 alir. Boylece `xdg-open`
komut satirindaki tek kullanimlik bilet (/proc/*/cmdline) baska bir yerel
kullanici tarafindan once tuketilemez. /proc okunamiyorsa KAPALI davranilir.
Host basligi yalnizca gercek yerel ad + bagli portu kabul eder
(DNS rebinding: dusman alan adi 127.0.0.1'e cozulse bile token'i okuyamaz).
Sayfa token GOMULMEZ: tarayici tek kullanimlik biletle acilir
(http://127.0.0.1:PORT/#s=<nonce>), bilet POST /api/session ile bir kez
token'a cevrilir. Bilet yalniz Unix soketindeki `web-session` komutundan
gelir; sayfayi okuyabilen baska bir yerel kullanici API'yi kullanamaz.
/api/i18n herkese aciktir (ceviri metinleri gizli degil); diger /api ve
/metrics cagrilari X-FerDim-Token ister (bayt bazinda sabit zamanli
karsilastirma, paylasilan tutucudan okunur). Yazma amacli POST /api
cagrilarinda Origin de yerel olmali; curl gibi Origin gondermeyen istemciler
token ile gecer. Govde 1 MiB ile sinirlidir ve application/json ister; erken
retler baglantiyi kapatir, boylece okunmamis govde bir sonraki istegin
basligi sanilmaz (istek kaymasi).
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from urllib.parse import parse_qs, unquote, urlparse

from .. import api
from .. import metrics as _metrics

MAX_BODY = 1024 * 1024       # 1 MiB: API govdesi
LOG = logging.getLogger("ferdim")
_SELF_UID = os.getuid()
_PEER_WARNED = threading.Event()


# ------------------------------------------------------------ peer uid kapisi
# Baska bir yerel kullanici (ya da onun adina calisan bir surec) 127.0.0.1
# portuna baglanabilir: `xdg-open` komut satirindaki #s=<bilet> adresi
# /proc/<pid>/cmdline'dan okunup once tuketilebilirdi. Bu yuzden baglantiyi
# acan surecin uid'i /proc/net/tcp(|6) uzerinden bulunur ve os.getuid() ile
# karsilastirilir. Bulunamazsa KAPALI davranilir (fail closed).

def _decode_proc_addr(text: str, v6: bool) -> tuple[str, int]:
    """`/proc/net/tcp` adres alani: '0100007F:1F90' -> ('127.0.0.1', 8080).

    Adres 32 bitlik sozcukler halinde kucuk endian yazilir (port ag
    duzeninde). Gecersiz metinde ValueError."""
    addr, _, port = text.partition(":")
    if v6:
        if len(addr) != 32:
            raise ValueError(text)
        raw = b"".join(struct.pack("=I", int(addr[i:i + 8], 16))
                       for i in range(0, 32, 8))
    else:
        if len(addr) != 8:
            raise ValueError(text)
        raw = struct.pack("=I", int(addr, 16))
    ip = ip_address(raw)
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped          # ::ffff:127.0.0.1 -> 127.0.0.1
    return str(ip), int(port, 16)


def parse_proc_net(text: str, v6: bool = False) -> list[tuple]:
    """`/proc/net/tcp[6]` metni -> [(yerel ip, port, uzak ip, port, uid)].

    Bozuk satirlar atilir; baslik satiri okunmaz. Saf islev: testler sentetik
    metin verir, gercek dosyaya ihtiyac yok."""
    rows = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            lip, lport = _decode_proc_addr(fields[1], v6)
            rip, rport = _decode_proc_addr(fields[2], v6)
            uid = int(fields[7])
        except ValueError:
            continue
        rows.append((lip, lport, rip, rport, uid))
    return rows


def _norm_ip(value) -> str:
    ip = ip_address(str(value))
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def peer_uid(client_addr, server_addr, proc: str = "/proc"):
    """Baglantiyi acan yerel surecin uid'i; bulunamazsa None.

    Eslesme: local_address == istemci (ip:port) ve rem_address == sunucu
    (ip:port) olan satir istemci soketidir, uid sutunu onundur. IPv6 ve
    v4-eslenmis satirlar icin tcp6 da okunur."""
    try:
        cip, cport = _norm_ip(client_addr[0]), int(client_addr[1])
        sip, sport = _norm_ip(server_addr[0]), int(server_addr[1])
    except (IndexError, TypeError, ValueError):
        return None
    for name, v6 in (("tcp", False), ("tcp6", True)):
        try:
            with open(os.path.join(proc, "net", name),
                      encoding="ascii", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for lip, lport, rip, rport, uid in parse_proc_net(text, v6):
            if (lport, rport) == (cport, sport) and lip == cip and rip == sip:
                return uid
    return None


def _warn_peer_unknown() -> None:
    """Kapi kapali kaldi: bir kez uyar (her istekte log doldurmasin)."""
    if not _PEER_WARNED.is_set():
        _PEER_WARNED.set()
        LOG.warning("peer uid okunamadi (/proc/net/tcp); HTTP istekleri "
                    "reddediliyor")


def make_server(env, ctx, preferred_port: int) -> ThreadingHTTPServer:
    # Token tutucusu ctx'te kurulur; HTTP istekleri ayni nesneyi gorur ki
    # `token-rotate` kosan sunucuda hemen etkili olsun.
    api.web_access(ctx)
    # HTTP cagrilari icin kopya: ayni qm/env/cfg nesneleri, ek olarak yol
    # kisitlamasi ve tasiyici isareti (sokete ozel komutlar HTTP'de yok).
    hctx = dict(ctx, restrict_paths=True, transport="http")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30        # yavas/yari acik istemci is parcacigini tutmasin

        def log_message(self, fmt, *args):
            pass

        # ---------------------------------------------------------- helpers
        def _peer_ok(self) -> bool:
            """Baglantiyi acan surec ayni kullanici mi?

            Baglanti basina bir kez cozumlenir: keep-alive istekleri
            /proc/net/tcp'yi yeniden ayristirmaz. Cozumlenemezse KAPALI:
            bilinmeyen bir yerel surec API'ye erisemez."""
            cached = getattr(self, "_peer_ok_cache", None)
            if cached is None:
                try:
                    uid = peer_uid(self.client_address,
                                   self.connection.getsockname())
                except OSError:            # kapanmis soket: cozumlenemez
                    uid = None
                if uid is None:
                    _warn_peer_unknown()
                cached = self._peer_ok_cache = (uid == _SELF_UID)
            return cached

        def _local_hosts(self) -> tuple[str, str, str]:
            """Gercek bagli portla yerel Host degerleri; rebinding'i keser."""
            port = self.server.server_address[1]
            return (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")

        def _host_ok(self) -> bool:
            hosts = self.headers.get_all("Host") or []
            # tam olarak bir Host: cift baslik (smuggling) de reddedilir
            return (len(hosts) == 1
                    and hosts[0].strip() in self._local_hosts())

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True     # curl/CLI: Origin gondermez, token yeter
            return origin.strip() in {f"http://{h}" for h in self._local_hosts()}

        def _reject(self, code: int):
            # govdesiz ret: token ve sayfa icerigi sizmaz
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()

        def _send_json(self, obj, code=200, close=False):
            body = (json.dumps(obj, ensure_ascii=False)).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def _deny(self, code: int, error: str, code_str: str = ""):
            """Erken ret: JSON govde + baglanti kapanir. Kapanmazsa okunmamis
            govde bir sonraki istegin basi sanilir (istek kaymasi)."""
            body = {"ok": False, "error": error}
            if code_str:
                body["code"] = code_str
            self._send_json(body, code, close=True)

        def _send_file(self, path, ctype: str):
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                self._deny(404, "bulunamadi")
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _send_text(self, text: str, ctype: str):
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            # hmac.compare_digest(str, str) ASCII disi baslikta TypeError
            # atar; bayt karsilastirmasi 401 doner ve sunucu ayakta kalir.
            token = api.web_access(ctx).get_token()
            if not token:
                return False
            got = self.headers.get("X-FerDim-Token", "")
            return hmac.compare_digest(got.encode("utf-8"),
                                       token.encode("utf-8"))

        def _reply(self, cmd: str, params: dict):
            """api.handle'i cagirir; beklenmeyen hata disariya metin sizdirmaz."""
            try:
                self._send_json(api.handle(hctx, cmd, params))
            except Exception:
                LOG.exception("HTTP API hatasi: %s", cmd)
                self._send_json({"ok": False, "code": "internal"}, 500)

        # ------------------------------------------------------------ routes
        def do_GET(self):
            if not self._peer_ok():
                self._reject(403)          # baska kullanici: her yol kapali
                return
            if not self._host_ok():
                self._reject(421)
                return
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                # sayfa token tasimaz: tarayici bileti /api/session'da token'a
                # cevrilir, kalici token hicbir zaman HTML'e yazilmaz
                self._send_file(env.web_dir / "index.html",
                                "text/html; charset=utf-8")
            elif u.path == "/app.js":
                self._send_file(env.web_dir / "app.js",
                                "text/javascript; charset=utf-8")
            elif u.path == "/style.css":
                self._send_file(env.web_dir / "style.css",
                                "text/css; charset=utf-8")
            elif u.path == "/metrics":
                # Prometheus metin bicimi: ayni token basligi gerekir
                # (curl -H "X-FerDim-Token: ..."); sorgu dizesi kabul edilmez.
                if not self._authorized():
                    self._deny(401, "yetkisiz")
                    return
                self._send_text(_metrics.render(ctx["qm"]),
                                "text/plain; version=0.0.4; charset=utf-8")
            elif u.path == "/api/i18n":
                # ceviri metinleri gizli degil: token'siz okunabilir. Sayfa
                # "oturum yok" iletisimini de bu sozlukten kurar.
                params = {"lang": (parse_qs(u.query).get("lang") or [""])[0]}
                self._reply("i18n", params)
            elif u.path.startswith("/api/"):
                if not self._authorized():
                    self._deny(401, "yetkisiz")
                    return
                cmd = {
                    "/api/version": "version",
                    "/api/stats": "stats",
                    "/api/jobs": "stats",
                    "/api/config": "config-get",
                }.get(u.path)
                if not cmd:
                    self._deny(404, "bilinmeyen yol")
                    return
                self._reply(cmd, {})
            else:
                self._deny(404, "bulunamadi")

        def do_POST(self):
            if not self._peer_ok():
                self._reject(403)          # baska kullanici: her yol kapali
                return
            if not self._host_ok():
                self._reject(421)
                return
            if not self._origin_ok():
                self._reject(403)
                return
            u = urlparse(self.path)
            if not u.path.startswith("/api/"):
                self._deny(404, "bulunamadi")
                return
            if self.headers.get("Transfer-Encoding"):
                # parcali govde okunmuyor: sinirini bilemeyiz
                self._deny(411, "uzunluk gerekli", "length_required")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._deny(400, "gecersiz govde")
                return
            if length < 0:
                self._deny(400, "gecersiz govde")
                return
            if length > MAX_BODY:
                self._deny(413, "govde cok buyuk", "too_large")
                return
            ctype = (self.headers.get("Content-Type") or "")
            ctype = ctype.split(";")[0].strip().lower()
            if length and ctype != "application/json":
                self._deny(415, "JSON gerekli", "content_type")
                return
            try:
                raw = self.rfile.read(length) if length else b""
                params = json.loads(raw) if raw else {}
            except Exception:
                self._deny(400, "gecersiz govde")
                return
            if not isinstance(params, dict):
                self._deny(400, "gecersiz govde")
                return
            if u.path == "/api/session":
                # tek kullanimlik bilet -> token; token basligi GEREKMEZ ama
                # Host/Origin kapilari yukarida uygulandi
                token = api.web_access(ctx).consume(params.get("nonce"))
                if not token:
                    self._deny(403, "oturum bileti gecersiz", "session_invalid")
                    return
                self._send_json({"ok": True, "token": token})
                return
            if not self._authorized():
                self._deny(401, "yetkisiz")
                return
            table = {
                "/api/add": "add", "/api/add-batch": "add-batch",
                "/api/pause": "pause", "/api/resume": "resume",
                "/api/cancel": "cancel", "/api/restart": "restart",
                "/api/remove": "remove", "/api/limit": "limit",
                "/api/config": "config-set", "/api/turbo": "turbo",
                "/api/zip-preview": "zip-preview",
                "/api/archive-list": "archive-list",
                "/api/archive-extract": "archive-extract",
                "/api/archive-repair": "archive-repair",
                "/api/archive-status": "archive-status",
                "/api/clear-finished": "clear-finished",
                "/api/history": "history",
                "/api/history-stats": "history-stats",
                "/api/history-redownload": "history-redownload",
                "/api/history-clear": "history-clear",
                "/api/hosts": "hosts",
            }
            cmd = table.get(u.path)
            if not cmd:
                self._deny(404, "bilinmeyen yol")
                return
            # /api/pause/:id gibi son ekleri destekle
            suffix = u.path.split("/", 3)[-1] if u.path.count("/") > 2 else ""
            if cmd in ("pause", "resume", "cancel", "restart", "remove") \
                    and suffix and "id" not in params:
                params["id"] = unquote(suffix.split("/")[-1])
            self._reply(cmd, params)

    for port in range(preferred_port, preferred_port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            httpd.daemon_threads = True
            return httpd
        except OSError:
            continue
    raise RuntimeError("bos HTTP port bulunamadi")
