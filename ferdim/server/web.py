"""127.0.0.1 HTTP API + yerel web arayuzu.

Guvenlik: Host basligi yalnizca gercek yerel ad + bagli portu kabul eder
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .. import api
from .. import metrics as _metrics

MAX_BODY = 1024 * 1024       # 1 MiB: API govdesi
LOG = logging.getLogger("ferdim")


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
