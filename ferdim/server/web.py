"""127.0.0.1 HTTP API + yerel web arayuzu.

Guvenlik: tum /api cagrilari X-FerDim-Token basligi ister (sabit zaman
karsilastirmasi). Browser ayni origin oldugu icin token sayfaya gomulur;
dis origin'ler SOP + preflight'siz engellenir.
"""
from __future__ import annotations

import hmac
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import api


def make_server(env, ctx, preferred_port: int) -> ThreadingHTTPServer:
    token = Path(env.token_path).read_text().strip()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        # ---------------------------------------------------------- helpers
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

        def _authorized(self) -> bool:
            got = self.headers.get("X-FerDim-Token", "")
            return hmac.compare_digest(got, token)

        # ------------------------------------------------------------ routes
        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/" or u.path == "/index.html":
                self._send_file(env.web_dir / "index.html", "text/html; charset=utf-8",
                                replace={"__TOKEN__": token})
            elif u.path == "/app.js":
                self._send_file(env.web_dir / "app.js",
                                "text/javascript; charset=utf-8")
            elif u.path == "/style.css":
                self._send_file(env.web_dir / "style.css", "text/css; charset=utf-8")
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
                "/api/clear-finished": "clear-finished",
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
