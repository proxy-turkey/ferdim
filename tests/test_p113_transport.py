"""ROADMAP 1.13 tasima: kaynak basina keep-alive baglanti yeniden kullanimi.

Kapsam:
  * Havuz: ardisik kisa Range istekleri ayni TCP baglantisini kullanir;
    yarim okunmus govde, `Connection: close` yaniti ve kesik akis havuza
    DONMEZ; sunucu bosta baglantiyi dusuruse istek taze baglantiyla
    sessizce yinelenir (GET tekrarlanabilir).
  * TLS: ayni ayarla tek el sikismasi, ayri ayarlar baglanti PAYLASMAZ ve
    yeni baglanti onceki oturumu yeniden kullanir (sunucu tarafi
    `session_reused`).
  * Havuz sinirlari (anahtar basina + toplam) ve havuz bosaltilinca fd
    artigi kalmamasi.
  * Yonlendirme, HTTP hata eslemesi, Range dogrulamasi, TCP_INFO soketi
    ve kimlik kapsami havuz yolunda da korunur.
  * FTP: EMFILE/ENFILE `fd_exhausted` kodu (job katmani bunu kalici
    altyapi hatasi sayar; yeniden denemez).

Butun ag testleri yerel sunucular kullanir (internet yok). Sertifikalar
`tests/test_p112_transport.py` ureticisiyle olusturulur (SKI/AKI dahil).
"""
from __future__ import annotations

import errno
import http.client
import os
import ssl
import sys
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim.engine import ftp as ftp_mod
from ferdim.engine import httpcli
from tests.helpers import tmp_root
from tests.test_p112_transport import OPENSSL, _Certs

BODY = b"0123456789" * 8192       # 80 KB
END = 99                          # kisa Range: havuzun hedefledigi durum
# Yola gore davranis: her test kendi sunucusunu kurar, mod haritasi sabittir.
_MODES = {
    "/close.bin": "close",        # yanitta Connection: close
    "/trunc.bin": "truncated",    # Content-Length yalan soyler
    "/ignore.bin": "ignore-range",   # Range'i yoksayar (200 tam govde)
    "/drop.bin": "drop",          # yanit tam, sonra baglanti dusurulur
    "/red.bin": "redirect",       # srv.redirect_to adresine 302
    "/loop": "loop",              # kendine 302
    "/yok.bin": "missing",        # 404
}


# ------------------------------------------------------------ yerel sunucu
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.live += 1
            self.server.sessions.append(
                bool(getattr(self.connection, "session_reused", False)))

    def finish(self):
        try:
            super().finish()
        finally:
            with self.server.lock:
                self.server.live -= 1
            self.server.closed.set()

    def do_GET(self):
        srv = self.server
        path = self.path.split("?", 1)[0]
        with srv.lock:
            srv.requests.append(path)
            srv.seen.append({k.lower(): v for k, v in self.headers.items()})
        mode = srv.modes.get(path) or _MODES.get(path, "range")
        if mode == "missing":
            self._send(404, b"yok")
        elif mode == "ignore-range":
            self._send(200, srv.body)
        elif mode == "close":
            self._send(200, srv.body, [("Connection", "close")])
            self.close_connection = True
        elif mode == "truncated":
            # Content-Length yalan soyler: govde yarim kalir
            self.send_response(200)
            self.send_header("Content-Length", str(END + 1))
            self.end_headers()
            self.wfile.write(b"x" * 10)
            self.wfile.flush()
            self.close_connection = True
        elif mode == "drop":
            # yanit tam, sonra sunucu baglantiyi dusurur (bosta soket olur)
            self._send(200, srv.body)
            self.close_connection = True
        elif mode == "redirect":
            self._send(302, b"", [("Location", srv.redirect_to)])
        elif mode == "loop":
            self._send(302, b"", [("Location", path)])
        else:
            self._range(srv.body)

    def _range(self, body: bytes) -> None:
        raw = self.headers.get("Range") or ""
        if raw.startswith("bytes="):
            first, _, last = raw[6:].partition("-")
            try:
                start, stop = int(first), int(last or first)
            except ValueError:
                start, stop = 0, len(body) - 1
            stop = min(stop, len(body) - 1)
            self._send(206, body[start:stop + 1],
                       [("Content-Range",
                         f"bytes {start}-{stop}/{len(body)}")])
        else:
            self._send(200, body)

    def _send(self, code: int, body: bytes, extra=()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        for key, val in extra:
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(body)


class _PoolServer(ThreadingHTTPServer):
    """Kabul edilen baglanti sayisini tutan ThreadingHTTPServer."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.body = BODY
        self.modes: dict = {}
        self.requests: list = []
        self.seen: list = []
        self.sessions: list = []
        self.live = 0
        self.accepted = 0
        self.redirect_to = ""
        self.lock = threading.Lock()
        self.closed = threading.Event()

    def handle_error(self, request, client_address):
        """Bilerek yarim birakilan baglantilar test ciktisini kirletmesin."""

    def get_request(self):
        conn, addr = super().get_request()
        with self.lock:
            self.accepted += 1
        return conn, addr


class _Server:
    def __init__(self, tls_ctx: ssl.SSLContext | None = None) -> None:
        self.httpd = _PoolServer()
        self.tls = tls_ctx is not None
        if tls_ctx is not None:
            self.httpd.socket = tls_ctx.wrap_socket(self.httpd.socket,
                                                    server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        scheme = "https" if self.tls else "http"
        return f"{scheme}://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def wait_live(self, deadline: float = 5.0) -> None:
        """Sunucudaki baglantilar kapanana kadar (en fazla deadline) bekle."""
        end = time.monotonic() + deadline
        while self.httpd.live and time.monotonic() < end:
            time.sleep(0.01)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


def _get_range(srv: _Server, path: str, opt: httpcli.DlOptions,
               start: int = 0) -> bytes:
    """Isteği acar ve govdeyi SONUNA kadar okur (havuza donme kosulu)."""
    with httpcli.open_range(srv.url + path, opt, start, END, timeout=5) as res:
        return res.read()


def _fds() -> int:
    return len(os.listdir("/proc/self/fd"))


# ------------------------------------------------------------------ havuz
class PoolReuseTests(unittest.TestCase):
    def setUp(self):
        httpcli.close_pool()
        self.srv = _Server()
        self.addCleanup(self.srv.close)
        self.addCleanup(httpcli.close_pool)
        self.opt = httpcli.DlOptions(timeout=5)

    def get_range(self, path: str = "/r.bin", start: int = 0,
                  opt: httpcli.DlOptions | None = None) -> bytes:
        return _get_range(self.srv, path, opt or self.opt, start)

    # -- yeniden kullanim --------------------------------------------
    def test_short_ranges_reuse_one_connection(self):
        """20 ardisik kisa Range tek baglanti kullanmali (tek el sikismasi)."""
        for _ in range(20):
            self.assertEqual(len(self.get_range()), END + 1)
        self.assertLessEqual(
            self.srv.httpd.accepted, 2,
            f"20 Range icin {self.srv.httpd.accepted} baglanti acildi")
        self.assertEqual(len(self.srv.httpd.requests), 20)
        self.assertGreater(httpcli.pool_stats()["idle"], 0)

    def test_half_read_body_not_reused(self):
        """Govde bitmeden close(): soket atilir, istek yeni baglanti acar."""
        with httpcli.open_range(self.srv.url + "/r.bin", self.opt, 0, END,
                                timeout=5) as res:
            self.assertEqual(len(res.read(10)), 10)
        self.assertEqual(len(self.get_range()), END + 1)
        self.assertEqual(self.srv.httpd.accepted, 2,
                         "yarim okunmus soket yeniden kullanilmamali")

    def test_connection_close_response_not_reused(self):
        for _ in range(3):
            self.assertEqual(len(self.get_range("/close.bin")), len(BODY))
        self.assertEqual(self.srv.httpd.accepted, 3)

    def test_truncated_body_not_reused(self):
        """Content-Length tam tuketilmedi: akis kesik, soket guvenilmez."""
        with httpcli.open_range(self.srv.url + "/trunc.bin", self.opt, 0, END,
                                timeout=5) as res:
            self.assertEqual(len(res.read(4096)), 10)
        self.assertEqual(len(self.get_range()), END + 1)
        self.assertEqual(self.srv.httpd.accepted, 2)

    def test_idle_socket_closed_by_server_is_retried(self):
        """Sunucu bosta baglantiyi dusururse istek seffaf yinelenir."""
        self.srv.httpd.modes["/drop.bin"] = "drop"
        self.assertEqual(len(self.get_range("/drop.bin")), len(BODY))
        self.assertTrue(self.srv.httpd.closed.wait(5),
                        "sunucu baglantiyi kapatmadi")
        self.assertEqual(len(self.get_range("/drop.bin")), len(BODY),
                         "olu bosta soket istegi bozmamali")
        self.assertEqual(self.srv.httpd.accepted, 2)

    # -- yonlendirme ve hata eslemesi --------------------------------
    def test_redirect_followed_and_credentials_dropped(self):
        """Baska koken (baska port) 30x: kimlik gitmez, Range kalir."""
        other = _Server()
        self.addCleanup(other.close)
        self.srv.httpd.modes["/red.bin"] = "redirect"
        self.srv.httpd.redirect_to = other.url + "/r.bin"
        opt = httpcli.DlOptions(user="kullanici", password="parola",
                                cookies="oturum=1", timeout=5,
                                origin=httpcli.origin_of(self.srv.url))
        self.assertEqual(len(self.get_range("/red.bin", opt=opt)), END + 1)
        self.assertEqual(len(other.httpd.seen), 1, "yonlendirme izlenmedi")
        seen = other.httpd.seen[0]
        self.assertNotIn("authorization", seen, "parola yabanci kokene gitti")
        self.assertNotIn("cookie", seen, "cerez yabanci kokene gitti")
        self.assertEqual(seen["range"], f"bytes=0-{END}")

    def test_redirect_within_origin_keeps_credentials(self):
        self.srv.httpd.modes["/red.bin"] = "redirect"
        self.srv.httpd.redirect_to = self.srv.url + "/r.bin"
        opt = httpcli.DlOptions(user="u", password="p", timeout=5,
                                origin=httpcli.origin_of(self.srv.url))
        self.assertEqual(len(self.get_range("/red.bin", opt=opt)), END + 1)
        seen = self.srv.httpd.seen[-1]
        self.assertIn("authorization", seen, "ayni koken kimligi korunmali")
        self.assertEqual(seen["range"], f"bytes=0-{END}")

    def test_redirect_loop_is_bounded(self):
        self.srv.httpd.modes["/loop"] = "loop"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get_range("/loop")
        self.assertEqual(ctx.exception.code, 302)
        ctx.exception.close()
        self.assertLessEqual(len(self.srv.httpd.requests),
                             httpcli.MAX_REPEATS + 2,
                             "yonlendirme dongusu sinirsiz surdu")

    def test_http_error_still_raised(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get_range("/yok.bin")
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

    def test_range_ignored_still_invalid(self):
        """206 yerine 200: start>0 ise RangeInvalid (mevcut davranis)."""
        self.srv.httpd.modes["/ignore.bin"] = "ignore-range"
        with self.assertRaises(httpcli.RangeInvalid):
            self.get_range("/ignore.bin", start=100)

    # -- arayuz uyumu -------------------------------------------------
    def test_probe_and_tcpinfo_socket(self):
        p = httpcli.probe(self.srv.url + "/r.bin", self.opt)
        self.assertTrue(p.accept_ranges)
        self.assertEqual(p.total, len(BODY))
        # yeniden kullanim TCP_INFO orneklemesini bozmamali: soket gorunur
        httpcli.close_pool()
        before = self.srv.httpd.accepted
        self.get_range()
        with httpcli.open_range(self.srv.url + "/r.bin", self.opt, 0, END,
                                timeout=5) as res:
            sock = httpcli.response_socket(res)
            self.assertIsNotNone(sock, "TCP_INFO soketi bulunamadi")
            self.assertTrue(callable(sock.getsockopt))
            self.assertEqual(len(res.read(END + 1)), END + 1)
        self.assertEqual(self.srv.httpd.accepted, before + 1,
                         "havuzdaki baglanti kullanilmali")

    # -- havuz sinirlari ---------------------------------------------
    def test_pool_bounds_honoured(self):
        pool = httpcli._pool()
        saved = (pool.per_key, pool.max)
        self.addCleanup(setattr, pool, "per_key", saved[0])
        self.addCleanup(setattr, pool, "max", saved[1])
        pool.clear()
        pool.per_key, pool.max = 2, 3
        key = ("http", "127.0.0.1", self.srv.port, None)
        for _ in range(5):
            conn = http.client.HTTPConnection("127.0.0.1", self.srv.port,
                                              timeout=5)
            conn.connect()
            pool.put(key, conn)
        self.assertEqual(pool.stats()["idle"], 2, "anahtar basina tavan")
        other = ("http", "127.0.0.1", 1, None)
        for _ in range(3):
            conn = http.client.HTTPConnection("127.0.0.1", self.srv.port,
                                              timeout=5)
            conn.connect()
            pool.put(other, conn)
        self.assertLessEqual(pool.stats()["idle"], 3, "toplam tavan")
        pool.clear()
        self.assertEqual(pool.stats()["idle"], 0)

    def test_pool_does_not_leak_fds(self):
        self.get_range()                 # isinma
        httpcli.close_pool()
        self.srv.wait_live()
        base = _fds()
        for _ in range(10):
            self.get_range()
        self.assertGreater(httpcli.pool_stats()["idle"], 0)
        httpcli.close_pool()
        self.assertEqual(httpcli.pool_stats()["idle"], 0)
        self.srv.wait_live()
        self.assertLessEqual(_fds(), base,
                             "havuz bosaltildiktan sonra fd artigi kaldi")

    # -- kapsam -------------------------------------------------------
    def test_proxy_and_socks_paths_not_pooled(self):
        with mock.patch.object(httpcli, "_env_proxy", return_value=False):
            self.assertTrue(httpcli._poolable("http://ornek.test/x.bin",
                                              httpcli.DlOptions()))
        for proxy in ("http://127.0.0.1:3128", "socks5://127.0.0.1:1080",
                      "socks5h://127.0.0.1:1080"):
            self.assertFalse(
                httpcli._poolable("http://ornek.test/x.bin",
                                  httpcli.DlOptions(proxy=proxy)),
                f"{proxy} havuzlanmamali")
        # ortam degiskeni gecidi de eski yola birakir
        with mock.patch.object(httpcli, "_env_proxy", return_value=True):
            self.assertFalse(httpcli._poolable("https://ornek.test/x.bin",
                                               httpcli.DlOptions()))
        # ftp/file gibi semalar bu yoldan gitmez
        self.assertFalse(httpcli._poolable("ftp://ornek.test/x.bin",
                                           httpcli.DlOptions()))


# -------------------------------------------------------------------- TLS
@unittest.skipUnless(OPENSSL, "openssl komutu yok")
class TlsPoolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tmp_root(cls, "ferdim-p113-tls-")
        cls.certs = _Certs(cls.root)
        httpcli._context_for.cache_clear()

    @classmethod
    def tearDownClass(cls):
        httpcli.close_pool()
        httpcli._context_for.cache_clear()

    def setUp(self):
        httpcli.close_pool()
        self.addCleanup(httpcli.close_pool)
        self.opt = httpcli.DlOptions(
            tls=httpcli.TlsConfig(ca_file=str(self.certs.ca)), timeout=5)

    def tls_ctx(self, maximum=None) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(self.certs.server), str(self.certs.server_key))
        if maximum is not None:
            ctx.maximum_version = maximum
        return ctx

    def server(self, **kw) -> _Server:
        srv = _Server(self.tls_ctx(**kw))
        self.addCleanup(srv.close)
        return srv

    def test_https_ranges_reuse_connection(self):
        srv = self.server()
        for _ in range(5):
            self.assertEqual(len(_get_range(srv, "/r.bin", self.opt)), END + 1)
        self.assertEqual(srv.httpd.accepted, 1,
                         "ardisik HTTPS Range'leri tek baglanti olmali")

    def test_distinct_tls_settings_do_not_share(self):
        """Ayni sunucu, ayri TLS ayarlari: ayri el sikismasi."""
        srv = self.server()
        insecure = httpcli.DlOptions(insecure=True, timeout=5)
        for opt in (self.opt, self.opt, insecure, insecure):
            self.assertEqual(len(_get_range(srv, "/r.bin", opt)), END + 1)
        self.assertEqual(srv.httpd.accepted, 2,
                         "TLS ayarlari baglanti havuzunu paylasmamali")

    def test_tls_session_resumed_on_new_connection(self):
        """Yeni baglanti onceki oturumu kullanmali (kisa el sikismasi)."""
        srv = self.server(maximum=ssl.TLSVersion.TLSv1_2)
        opt = httpcli.DlOptions(
            tls=httpcli.TlsConfig(ca_file=str(self.certs.ca),
                                  min_version="1.2"), timeout=5)
        self.assertEqual(len(_get_range(srv, "/r.bin", opt)), END + 1)
        self.assertFalse(srv.httpd.sessions[0])
        httpcli.close_pool(keep_sessions=True)   # soketi at, oturumu koru
        self.assertEqual(len(_get_range(srv, "/r.bin", opt)), END + 1)
        self.assertEqual(srv.httpd.accepted, 2, "yeni baglanti kurulmali")
        self.assertTrue(srv.httpd.sessions[1],
                        "TLS oturumu yeniden kullanilmadi")


# -------------------------------------------------------------------- FTP
class FtpFdTests(unittest.TestCase):
    def test_emfile_wrapped_as_fd_exhausted(self):
        """fd tukenmesi yeniden denenmez: kodu fd_exhausted olmali."""
        for err in (errno.EMFILE, errno.ENFILE):
            wrapped = ftp_mod._wrap(OSError(err, os.strerror(err)),
                                    connecting=True)
            self.assertEqual(wrapped.code, "fd_exhausted", os.strerror(err))
            self.assertFalse(wrapped.transient,
                             "fd tukenmesi yeniden denenmemeli")
            self.assertIn("tanitici", str(wrapped))

    def test_other_os_errors_keep_old_mapping(self):
        lost = ftp_mod._wrap(OSError(errno.EIO, "io"))
        self.assertEqual(lost.code, "unknown")
        self.assertTrue(lost.transient)
        refused = ftp_mod._wrap(OSError(errno.ECONNREFUSED, "red"),
                                connecting=True)
        self.assertEqual(refused.code, "network")


if __name__ == "__main__":
    unittest.main()
