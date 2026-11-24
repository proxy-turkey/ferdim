"""Yerel web sunucusu guvenligi: DNS rebinding (Host) ve Origin kapisi,
token karsilastirmasinin ASCII disi baslikta cokmemesi.

Rebinding saldirisi: dusman alan adi 127.0.0.1'e cozulur, tarayici ayni
origin'i korur ve Host basligi saldirganin adidir. Sunucu Host'u gercek
bagli portla karsilastirdigi icin token'in gomuldugu sayfa verilmez.
"""
from __future__ import annotations

import socket
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim.engine.queue import QueueManager
from ferdim.paths import AppEnv
from ferdim.server.web import make_server
from tests.helpers import tmp_root

TOKEN = "testtoken"


def _request(path="/", method="GET", host=None, token=None, origin=None,
             body=b"") -> bytes:
    lines = [f"{method} {path} HTTP/1.1", "Connection: close"]
    if host is not None:
        lines.append(f"Host: {host}")
    if token is not None:
        lines.append(f"X-FerDim-Token: {token}")
    if origin is not None:
        lines.append(f"Origin: {origin}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
        lines.append("Content-Type: application/json")
    return "\r\n".join(lines).encode("latin-1") + b"\r\n\r\n" + body


def _raw(port: int, payload: bytes) -> tuple[int, bytes]:
    """Ham HTTP istegi: urllib basliklari yeniden yaziyor, Host'u biz kurariz."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(payload)
        chunks = []
        while True:
            part = s.recv(65536)
            if not part:
                break
            chunks.append(part)
    data = b"".join(chunks)
    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, body


class WebHostGuardTests(unittest.TestCase):
    """Host zorunlu yerel; yazan POST'larda Origin de yerel olmali."""

    @classmethod
    def setUpClass(cls):
        cls.env = AppEnv(root=tmp_root(cls, "ferdim-websec-"))
        cls.cfg = cls.env.load_config()
        cls.qm = QueueManager(cls.env, cls.cfg)
        cls.addClassCleanup(cls.qm.shutdown)
        cls.ctx = {"qm": cls.qm, "env": cls.env, "cfg": cls.cfg,
                   "set_cfg": cls.env.save_config}
        cls.env.token_path.write_text(TOKEN + "\n")
        cls.httpd = make_server(cls.env, cls.ctx, 0)
        cls.port = cls.httpd.server_address[1]
        cls.addClassCleanup(cls.httpd.server_close)
        cls.addClassCleanup(cls.httpd.shutdown)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.host = f"127.0.0.1:{cls.port}"

    def test_01_wrong_host_rejected_on_every_route(self):
        # Host yanlis/eksik: token'li istek bile govdesiz 421 alir
        for path in ("/", "/app.js", "/api/stats", "/metrics", "/bilinmeyen"):
            for host in ("evil.example:1234", f"127.0.0.1:{self.port + 1}",
                         "127.0.0.1", None):
                with self.subTest(path=path, host=host):
                    status, body = _raw(self.port, _request(
                        path=path, host=host, token=TOKEN))
                    self.assertEqual(status, 421)
                    self.assertEqual(body, b"")
                    self.assertNotIn(TOKEN.encode(), body)

    def test_01b_duplicate_host_rejected(self):
        # iki Host basligi: hangisinin okunacagi belirsiz, kabul edilmez
        payload = (b"GET / HTTP/1.1\r\nConnection: close\r\n"
                   + f"Host: {self.host}\r\n".encode()
                   + b"Host: evil.example\r\n\r\n")
        status, body = _raw(self.port, payload)
        self.assertEqual(status, 421)
        self.assertEqual(body, b"")

    def test_02_local_hosts_accepted(self):
        for host in (self.host, f"localhost:{self.port}",
                     f"[::1]:{self.port}"):
            with self.subTest(host=host):
                status, body = _raw(self.port, _request("/", host=host))
                self.assertEqual(status, 200)
                self.assertIn(TOKEN.encode(), body)
                status, body = _raw(self.port, _request(
                    "/api/stats", host=host, token=TOKEN))
                self.assertEqual(status, 200)
                self.assertIn(b'"jobs"', body)

    def test_03_post_origin_guard(self):
        payload = b'{"id": "yok"}'
        # capraz origin: govdesiz 403
        for origin in ("https://evil.example", "http://evil.example",
                       f"http://127.0.0.1:{self.port + 1}", "null"):
            with self.subTest(origin=origin):
                status, body = _raw(self.port, _request(
                    "/api/remove", method="POST", host=self.host,
                    token=TOKEN, origin=origin, body=payload))
                self.assertEqual(status, 403)
                self.assertEqual(body, b"")
        # ayni origin (web arayuzu) ve Origin gondermeyen curl kabul
        for host, origin in ((self.host, f"http://{self.host}"),
                             (f"localhost:{self.port}",
                              f"http://localhost:{self.port}"),
                             (self.host, None)):
            with self.subTest(origin=origin):
                status, body = _raw(self.port, _request(
                    "/api/remove", method="POST", host=host,
                    token=TOKEN, origin=origin, body=payload))
                self.assertEqual(status, 200, body)
                self.assertIn(b'"ok"', body)

    def test_04_non_ascii_token_header_is_401(self):
        # hmac.compare_digest(str, str) bu baslikta TypeError atiyordu:
        # istek yanitsiz kapaniyordu. Artik 401 ve sunucu ayakta.
        payload = (f"GET /api/stats HTTP/1.1\r\nHost: {self.host}\r\n"
                   "Connection: close\r\n").encode() + \
            b"X-FerDim-Token: t\xf6k\xe9n\r\n\r\n"
        status, body = _raw(self.port, payload)
        self.assertEqual(status, 401)
        self.assertNotIn(b"jobs", body)
        # ayni baglanti/sunucu sonraki dogru istege cevap verir
        status, body = _raw(self.port, _request(
            "/api/stats", host=self.host, token=TOKEN))
        self.assertEqual(status, 200)
        self.assertIn(b'"jobs"', body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
