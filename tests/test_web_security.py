"""Yerel web sunucusu guvenligi: DNS rebinding (Host) ve Origin kapisi,
token karsilastirmasinin ASCII disi baslikta cokmemesi, sayfaya token
gomulmemesi.

Rebinding saldirisi: dusman alan adi 127.0.0.1'e cozulur, tarayici ayni
origin'i korur ve Host basligi saldirganin adidir. Sunucu Host'u gercek
bagli portla karsilastirir. Sayfa artik token tasimaz: tarayici tek
kullanimlik biletle acilir (bkz. tests/test_web_session.py), sayfayi okuyan
baska bir yerel kullanici API'ye erisemez.
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
             body=b"", ctype="application/json", length=None) -> bytes:
    lines = [f"{method} {path} HTTP/1.1", "Connection: close"]
    if host is not None:
        lines.append(f"Host: {host}")
    if token is not None:
        lines.append(f"X-FerDim-Token: {token}")
    if origin is not None:
        lines.append(f"Origin: {origin}")
    if body or length is not None:
        lines.append(f"Content-Length: {len(body) if length is None else length}")
        if ctype is not None:
            lines.append(f"Content-Type: {ctype}")
    return "\r\n".join(lines).encode("latin-1") + b"\r\n\r\n" + body


def _raw_full(port: int, payload: bytes) -> tuple[int, dict, bytes]:
    """Ham HTTP istegi: urllib basliklari yeniden yaziyor, Host'u biz kurariz.
    (durum, basliklar, govde) dondurur; baslik adlari kucuk harftir."""
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
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(b":")
        headers[k.strip().lower().decode()] = v.strip().decode()
    return status, headers, body


def _raw(port: int, payload: bytes) -> tuple[int, bytes]:
    status, _headers, body = _raw_full(port, payload)
    return status, body


def json_code(body: bytes) -> str:
    """Yanit govdesindeki kararli hata kodunu okur (test yardimcisi)."""
    import json
    try:
        return json.loads(body).get("code", "")
    except Exception:
        return ""


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

    def test_02_local_hosts_accepted_page_has_no_token(self):
        for host in (self.host, f"localhost:{self.port}",
                     f"[::1]:{self.port}"):
            with self.subTest(host=host):
                status, body = _raw(self.port, _request("/", host=host))
                self.assertEqual(status, 200)
                # sayfa token GOMMEZ: yerel kullanici sayfayi okusa da
                # API'yi kullanamaz (bilet yalniz Unix soketinden gelir)
                self.assertNotIn(TOKEN.encode(), body)
                self.assertNotIn(b"__TOKEN__", body)
                self.assertNotIn(b"FERDIM_TOKEN", body)
                status, body = _raw(self.port, _request(
                    "/api/stats", host=host, token=TOKEN))
                self.assertEqual(status, 200)
                self.assertIn(b'"jobs"', body)
                # token'siz ayni yol 401
                status, body = _raw(self.port, _request(
                    "/api/stats", host=host))
                self.assertEqual(status, 401)

    def test_02b_index_is_served_verbatim(self):
        # sunucu sayfayi oldugu gibi verir (yer tutucu degistirme yok)
        raw = (self.env.web_dir / "index.html").read_bytes()
        status, _headers, body = _raw_full(
            self.port, _request("/index.html", host=self.host))
        self.assertEqual(status, 200)
        self.assertEqual(body, raw)

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

    def test_05_post_requires_json_and_closes(self):
        # icerik turu yanlis: 415, baglanti kapanir (okunmamis govde kalmasin)
        status, headers, body = _raw_full(self.port, _request(
            "/api/pause", method="POST", host=self.host, token=TOKEN,
            body=b'{"id": "x"}', ctype="text/plain"))
        self.assertEqual(status, 415)
        self.assertEqual(headers.get("connection"), "close")
        self.assertEqual(json_code(body), "content_type")
        # govde var, Content-Type hic yok: ayni ret
        status, _headers, body = _raw_full(self.port, _request(
            "/api/pause", method="POST", host=self.host, token=TOKEN,
            body=b'{"id": "x"}', ctype=None))
        self.assertEqual(status, 415)
        # parametreli JSON kabul edilir
        status, body = _raw(self.port, _request(
            "/api/pause", method="POST", host=self.host, token=TOKEN,
            body=b'{"id": "yok"}', ctype="application/json; charset=utf-8"))
        self.assertEqual(status, 200, body)

    def test_06_body_cap(self):
        # 1 MiB ustu govde okunmadan reddedilir: 413 + kapanis
        status, headers, body = _raw_full(self.port, _request(
            "/api/add", method="POST", host=self.host, token=TOKEN,
            length=1024 * 1024 + 1, body=b""))
        self.assertEqual(status, 413)
        self.assertEqual(headers.get("connection"), "close")
        self.assertEqual(json_code(body), "too_large")
        # tam sinir kabul edilir (govde gercekten gonderilir)
        filler = b" " * (1024 * 1024 - 2)
        status, _headers, _body = _raw_full(self.port, _request(
            "/api/clear-finished", method="POST", host=self.host, token=TOKEN,
            body=b"{}" + filler))
        self.assertNotEqual(status, 413)

    def test_07_early_rejects_close_connection(self):
        cases = {
            # 401: token yok
            "401": _request("/api/stats", host=self.host),
            # 404: bilinmeyen API yolu (token'li)
            "404": _request("/api/yok", host=self.host, token=TOKEN),
            # 403: capraz origin
            "403": _request("/api/pause", method="POST", host=self.host,
                            token=TOKEN, body=b"{}",
                            origin="https://evil.example"),
            # 413: govde siniri
            "413": _request("/api/pause", method="POST", host=self.host,
                            token=TOKEN, length=2 * 1024 * 1024, body=b""),
            # 415: icerik turu
            "415": _request("/api/pause", method="POST", host=self.host,
                            token=TOKEN, body=b"{}", ctype="text/plain"),
        }
        for name, payload in cases.items():
            with self.subTest(code=name):
                status, headers, body = _raw_full(self.port, payload)
                self.assertEqual(status, int(name))
                self.assertEqual(headers.get("connection"), "close", name)
                self.assertNotIn(b"jobs", body)
                # erken rette govde JSON'dur; token asla sizmaz
                self.assertNotIn(TOKEN.encode(), body)

    def test_08_internal_error_hides_text(self):
        from ferdim import api
        real = api.handle

        def boom(_ctx, _cmd, _params):
            raise RuntimeError("gizli-ayrinti-123")

        api.handle = boom
        try:
            status, _headers, body = _raw_full(self.port, _request(
                "/api/pause", method="POST", host=self.host, token=TOKEN,
                body=b'{"id": "x"}'))
        finally:
            api.handle = real
        self.assertEqual(status, 500)
        self.assertEqual(json_code(body), "internal")
        self.assertNotIn(b"gizli-ayrinti-123", body)
        self.assertNotIn(b"RuntimeError", body)
        # sunucu ayakta: sonraki istek normal cevap alir
        status, body = _raw(self.port, _request(
            "/api/stats", host=self.host, token=TOKEN))
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
