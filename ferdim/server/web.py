"""127.0.0.1 HTTP API + yerel web arayuzu.

Guvenlik: Host basligi yalnizca gercek yerel ad + bagli portu kabul eder
(DNS rebinding: dusman alan adi 127.0.0.1'e cozulse bile token'i okuyamaz).
Tum /api ve /metrics cagrilari X-FerDim-Token ister (bayt bazinda sabit
zamanli karsilastirma). Yazma amacli POST /api cagrilarinda Origin de
yerel olmali; curl gibi Origin gondermeyen istemciler token ile gecer.
"""
from __future__ import annotations

import hmac
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import api
from .. import metrics as _metrics


def make_server(env, ctx, preferred_port: int) -> ThreadingHTTPServer:
    token = Path(env.token_path).read_text().strip()
    token_b = token.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

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
            self.end_headers()

        def _send_json(self, obj, code=200):
            body = (json.dumps(obj, ensure_ascii=False)).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, ctype: str, replace=None):
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                self._send_json({"ok": False, "error": "bulunamadi"}, 404)
                return
            if replace:
                for k, v in replace.items():
                    data = data.replace(k.encode(), v.encode())
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
            got = self.headers.get("X-FerDim-Token", "")
            return hmac.compare_digest(got.encode("utf-8"), token_b)

        # ------------------------------------------------------------ routes
        def do_GET(self):
            if not self._host_ok():
                self._reject(421)
                return
            u = urlparse(self.path)
            if u.path == "/" or u.path == "/index.html":
                self._send_file(env.web_dir / "index.html", "text/html; charset=utf-8",
                                replace={"__TOKEN__": token})
            elif u.path == "/app.js":
                self._send_file(env.web_dir / "app.js",
                                "text/javascript; charset=utf-8")
            elif u.path == "/style.css":
                self._send_file(env.web_dir / "style.css", "text/css; charset=utf-8")
            elif u.path == "/metrics":
                # Prometheus metin bicimi: ayni token basligi gerekir
                # (curl -H "X-FerDim-Token: ..."); sorgu dizesi kabul edilmez.
                if not self._authorized():
                    self._send_json({"ok": False, "error": "yetkisiz"}, 401)
                    return
                self._send_text(_metrics.render(ctx["qm"]),
                                "text/plain; version=0.0.4; charset=utf-8")
            elif u.path.startswith("/api/"):
                if not self._authorized():
                    self._send_json({"ok": False, "error": "yetkisiz"}, 401)
                    return
                cmd = {
                    "/api/version": "version",
                    "/api/stats": "stats",
                    "/api/jobs": "stats",
                    "/api/config": "config-get",
                    "/api/i18n": "i18n",
                }.get(u.path)
                if not cmd:
                    self._send_json({"ok": False, "error": "bilinmeyen yol"}, 404)
                    return
                params = {}
                if cmd == "i18n":
                    qs = parse_qs(u.query)
                    params["lang"] = (qs.get("lang") or [""])[0]
                self._send_json(api.handle(ctx, cmd, params))
            else:
                self._send_json({"ok": False, "error": "bulunamadi"}, 404)

        def do_POST(self):
            if not self._host_ok():
                self._reject(421)
                return
            if not self._origin_ok():
                self._reject(403)
                return
            u = urlparse(self.path)
            if not u.path.startswith("/api/"):
                self._send_json({"ok": False, "error": "bulunamadi"}, 404)
                return
            if not self._authorized():
                self._send_json({"ok": False, "error": "yetkisiz"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                params = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                params = {}
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
                self._send_json({"ok": False, "error": "bilinmeyen yol"}, 404)
                return
            # /api/pause/:id gibi son ekleri destekle
            suffix = u.path.split("/", 3)[-1] if u.path.count("/") > 2 else ""
            if cmd in ("pause", "resume", "cancel", "restart", "remove") \
                    and suffix and "id" not in params:
                params["id"] = unquote(suffix.split("/")[-1])
            try:
                self._send_json(api.handle(ctx, cmd, params))
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)

    for port in range(preferred_port, preferred_port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            httpd.daemon_threads = True
            return httpd
        except OSError:
            continue
    raise RuntimeError("bos HTTP port bulunamadi")
