"""Unix socket JSON-satir RPC sunucusu + watch bildirimleri."""
from __future__ import annotations

import json
import socketserver
import threading


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        server: SockServer = self.server
        watcher = None
        try:
            for line in self.rfile:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    self.wfile.write(b'{"ok":false,"error":"gecersiz JSON"}\n')
                    continue
                cmd = msg.pop("cmd", "")
                req_id = msg.pop("id", None)
                if cmd == "watch":
                    watcher = self.wfile
                    server.register_watcher(self.wfile)
                    continue
                from .. import api
                try:
                    resp = api.handle(server.ctx, cmd, msg)
                except Exception as e:
                    resp = {"ok": False, "error": str(e)}
                if req_id is not None:
                    resp["req_id"] = req_id
                try:
                    self.wfile.write(
                        (json.dumps(resp, ensure_ascii=False) + "\n").encode())
                    self.wfile.flush()
                except Exception:
                    break
        except Exception:
            pass
        finally:
            if watcher is not None:
                server.unregister_watcher(watcher)


class SockServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, ctx: dict):
        self.ctx = ctx
        self._watch_lock = threading.Lock()
        self._watchers: list = []
        import os
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        super().__init__(path, _Handler)
        os.chmod(path, 0o600)
        qm = ctx["qm"]
        qm.add_listener(self._on_change)
        # periyodik stats itmesi (GUI/CLI watch)
        self._push_thread = threading.Thread(target=self._push_loop,
                                             daemon=True)
        self._push_thread.start()

    def register_watcher(self, wfile):
        with self._watch_lock:
            self._watchers.append(wfile)

    def unregister_watcher(self, wfile):
        with self._watch_lock:
            if wfile in self._watchers:
                self._watchers.remove(wfile)

    def _on_change(self, _job=None):
        self._push()

    def _push_loop(self):
        import time
        while True:
            time.sleep(1.0)
            self._push()

    def _push(self):
        from .. import api
        try:
            stats = api.handle(self.ctx, "stats", {})
        except Exception:
            return
        line = (json.dumps({"event": "stats", **stats},
                           ensure_ascii=False) + "\n").encode()
        with self._watch_lock:
            dead = []
            for w in self._watchers:
                try:
                    w.write(line)
                    w.flush()
                except Exception:
                    dead.append(w)
            for w in dead:
                self._watchers.remove(w)
