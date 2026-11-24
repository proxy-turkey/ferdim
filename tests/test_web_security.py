"""Yerel web sunucusu guvenligi: peer uid kapisi, DNS rebinding (Host) ve
Origin kapisi, token karsilastirmasinin ASCII disi baslikta cokmemesi,
sayfaya token gomulmemesi.

Rebinding saldirisi: dusman alan adi 127.0.0.1'e cozulur, tarayici ayni
origin'i korur ve Host basligi saldirganin adidir. Sunucu Host'u gercek
bagli portla karsilastirir. Sayfa artik token tasimaz: tarayici tek
kullanimlik biletle acilir (bkz. tests/test_web_session.py), sayfayi okuyan
baska bir yerel kullanici API'ye erisemez. Baglantiyi acan surecin uid'i
/proc/net/tcp(|6) uzerinden dogrulanir: baska kullanici 403 alir; boylece
`xdg-open` komut satirindaki #s=<bilet> adresi once tuketilemez.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim.engine.queue import QueueManager
from ferdim.paths import AppEnv
from ferdim.server import web
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


# ---------------------------------------------------------------- peer uid
PROC_HEADER = ("  sl  local_address rem_address   st tx_queue rx_queue tr "
               "tm->when retrnsmt   uid  timeout inode\n")


def proc_row(local: str, remote: str, uid: int = 1000) -> str:
    """Sentetik /proc/net/tcp satiri (alan sirasi gercek dosyayla ayni)."""
    return (f"   0: {local} {remote} 01 00000000:00000000 00:00000000 "
            f"00000000  {uid}        0 1 1 0000000000000000 100 0 0 10 0\n")


V4_CLIENT = "0100007F:9C40"          # 127.0.0.1:40000
V4_SERVER = "0100007F:1F90"          # 127.0.0.1:8080
V6_CLIENT = "00000000000000000000000001000000:9C40"     # [::1]:40000
V6_SERVER = "00000000000000000000000001000000:1F90"     # [::1]:8080
MAPPED_CLIENT = "0000000000000000FFFF00000100007F:9C40"  # ::ffff:127.0.0.1
MAPPED_SERVER = "0000000000000000FFFF00000100007F:1F90"


class ProcParserTests(unittest.TestCase):
    """`/proc/net/tcp` ayristirmasi: v4, v6 ve v4-eslenmis satirlar."""

    def test_01_ipv4_row(self):
        rows = web.parse_proc_net(
            PROC_HEADER + proc_row(V4_CLIENT, V4_SERVER, 1000))
        self.assertEqual(rows,
                         [("127.0.0.1", 40000, "127.0.0.1", 8080, 1000)])

    def test_02_ipv6_row(self):
        rows = web.parse_proc_net(
            PROC_HEADER + proc_row(V6_CLIENT, V6_SERVER, 1001), v6=True)
        self.assertEqual(rows, [("::1", 40000, "::1", 8080, 1001)])

    def test_03_v4_mapped_row_is_ipv4(self):
        rows = web.parse_proc_net(
            PROC_HEADER + proc_row(MAPPED_CLIENT, MAPPED_SERVER, 1002),
            v6=True)
        self.assertEqual(rows[0][0::2], ("127.0.0.1", "127.0.0.1", 1002))
        self.assertEqual(rows[0][1::2], (40000, 8080))

    def test_04_header_and_broken_rows_are_skipped(self):
        text = (PROC_HEADER + "bozuk satir\n"
                + proc_row("0100007F", V4_SERVER, 1)        # port yok
                + proc_row("ZZZZZZZZ:1F90", V4_SERVER, 1)   # hex degil
                + proc_row(V4_CLIENT, V4_SERVER, 1000))
        self.assertEqual(web.parse_proc_net(text),
                         [("127.0.0.1", 40000, "127.0.0.1", 8080, 1000)])
        # v6 ayristiricisi 8 haneli satiri kabul etmez
        self.assertEqual(web.parse_proc_net(
            PROC_HEADER + proc_row(V4_CLIENT, V4_SERVER), v6=True), [])


class PeerUidTests(unittest.TestCase):
    """peer_uid: eslesen satirin uid'i; yoksa/okunamiyorsa None (kapali)."""

    def _proc(self, v4: str = "", v6: str = ""):
        root = tmp_root(self, "ferdim-proc-")
        (root / "net").mkdir()
        (root / "net" / "tcp").write_text(PROC_HEADER + v4)
        (root / "net" / "tcp6").write_text(PROC_HEADER + v6)
        return root

    def test_01_ipv4_match_returns_uid(self):
        root = self._proc(v4=proc_row(V4_CLIENT, V4_SERVER, 4242))
        self.assertEqual(
            web.peer_uid(("127.0.0.1", 40000), ("127.0.0.1", 8080),
                         proc=str(root)), 4242)

    def test_02_ipv6_match_returns_uid(self):
        root = self._proc(v6=proc_row(V6_CLIENT, V6_SERVER, 4243))
        self.assertEqual(
            web.peer_uid(("::1", 40000), ("::1", 8080), proc=str(root)), 4243)

    def test_03_v4_mapped_row_matches_ipv4_client(self):
        root = self._proc(v6=proc_row(MAPPED_CLIENT, MAPPED_SERVER, 4244))
        self.assertEqual(
            web.peer_uid(("::ffff:127.0.0.1", 40000), ("127.0.0.1", 8080),
                         proc=str(root)), 4244)

    def test_04_no_matching_row_is_none(self):
        root = self._proc(v4=proc_row(V4_CLIENT, V4_SERVER, 4242))
        self.assertIsNone(
            web.peer_uid(("127.0.0.1", 40001), ("127.0.0.1", 8080),
                         proc=str(root)))
        # ters yon (sunucu satiri) istemci sayilmaz
        self.assertIsNone(
            web.peer_uid(("127.0.0.1", 8080), ("127.0.0.1", 40000),
                         proc=str(root)))

    def test_05_unreadable_proc_is_none(self):
        self.assertIsNone(web.peer_uid(("127.0.0.1", 1), ("127.0.0.1", 2),
                                       proc="/tmp/ferdim-yok-boyle-dizin"))
        self.assertIsNone(web.peer_uid(("bozuk", 1), ("127.0.0.1", 2)))


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

    def test_09_foreign_uid_is_rejected_on_every_route(self):
        """Baska kullanicinin sureci (ya da onun adina calisan istemci)
        /proc/*/cmdline'daki bileti once tuketemez: her yol 403 + kapanis."""
        cases = (("/", "GET"), ("/app.js", "GET"), ("/metrics", "GET"),
                 ("/api/i18n", "GET"), ("/api/stats", "GET"),
                 ("/api/config", "GET"), ("/api/session", "POST"),
                 ("/api/config", "POST"), ("/bilinmeyen", "GET"))
        with mock.patch.object(web, "peer_uid", return_value=os.getuid() + 1):
            for path, method in cases:
                with self.subTest(path=path, method=method):
                    body = b'{"nonce": "x"}' if method == "POST" else b""
                    status, headers, out = _raw_full(self.port, _request(
                        path, method=method, host=self.host, token=TOKEN,
                        body=body))
                    self.assertEqual(status, 403)
                    self.assertEqual(headers.get("connection"), "close")
                    self.assertEqual(out, b"")
                    self.assertNotIn(TOKEN.encode(), out)
                    self.assertNotIn(b"jobs", out)

    def test_10_unknown_uid_fails_closed(self):
        """Cozumlenemeyen uid (proc okunamadi / satir yok) = KAPALI."""
        with mock.patch.object(web, "peer_uid", return_value=None):
            for path in ("/", "/api/stats", "/metrics"):
                with self.subTest(path=path):
                    status, headers, out = _raw_full(self.port, _request(
                        path, host=self.host, token=TOKEN))
                    self.assertEqual(status, 403)
                    self.assertEqual(headers.get("connection"), "close")
                    self.assertNotIn(b"jobs", out)

    def test_11_live_connection_maps_to_our_uid(self):
        """Gercek soket: /proc/net/tcp satiri bizim uid'imizi verir."""
        with socket.create_connection(("127.0.0.1", self.port),
                                      timeout=10) as s:
            self.assertEqual(
                web.peer_uid(s.getsockname(), s.getpeername()), os.getuid())

    def test_12_probe_is_cached_per_connection(self):
        """Keep-alive: ikinci istek /proc/net/tcp'yi yeniden ayristirmaz."""
        payload = (f"GET /api/i18n HTTP/1.1\r\nHost: {self.host}\r\n\r\n"
                   f"GET /api/i18n HTTP/1.1\r\nHost: {self.host}\r\n"
                   "Connection: close\r\n\r\n").encode()
        with mock.patch.object(web, "peer_uid", wraps=web.peer_uid) as spy:
            with socket.create_connection(("127.0.0.1", self.port),
                                          timeout=10) as s:
                s.sendall(payload)
                data = b""
                while True:
                    part = s.recv(65536)
                    if not part:
                        break
                    data += part
        self.assertEqual(data.count(b"HTTP/1.1 200"), 2, data[:300])
        self.assertEqual(spy.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
