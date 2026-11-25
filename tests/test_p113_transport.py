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
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim.engine import ftp as ftp_mod
from ferdim.engine import httpcli
from ferdim.engine import job as job_mod
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.paths import AppEnv
from tests.helpers import tmp_root
from tests.test_p112_transport import OPENSSL, _Certs

BODY = b"0123456789" * 8192       # 80 KB
END = 99                          # kisa Range: havuzun hedefledigi durum


def _wait_state(job, states, timeout: float = 30.0) -> bool:
    """Is `states` kumesine (en fazla timeout sn) ulasti mi."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if job.state in states:
            return True
        time.sleep(0.02)
    return False


# Yola gore davranis: her test kendi sunucusunu kurar, mod haritasi sabittir.
_MODES = {
    "/close.bin": "close",        # yanitta Connection: close
    "/trunc.bin": "truncated",    # Content-Length yalan soyler
    "/ignore.bin": "ignore-range",   # Range'i yoksayar (200 tam govde)
    "/drop.bin": "drop",          # yanit tam, sonra baglanti dusurulur
    "/red.bin": "redirect",       # srv.redirect_to adresine 302
    "/loop": "loop",              # kendine 302
    "/yok.bin": "missing",        # 404
    "/nocontent": "nocontent",    # 204 + (sozlesmeye aykiri) govde
    "/chunked": "chunked",        # Transfer-Encoding: chunked govde
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
        elif mode == "nocontent":
            # 204 sozlesmesi "govde yok" der; sunucu yine de bayt gonderiyor
            self.send_response(204)
            self.send_header("Content-Length", "32")
            self.end_headers()
            self.wfile.write(b"j" * 32)
            self.wfile.flush()
        elif mode == "chunked":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(srv.body), 4096):
                part = srv.body[i:i + 4096]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
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


# ------------------------------------------------ govde sozlesmesi (M1)
class BodyContractTests(unittest.TestCase):
    """204/304/1xx: http.client uzunlugu kosulsuz 0 sayar (httpclient M1)."""

    def setUp(self):
        httpcli.close_pool()
        self.srv = _Server()
        self.addCleanup(self.srv.close)
        self.addCleanup(httpcli.close_pool)
        self.opt = httpcli.DlOptions(timeout=5)

    def get_range(self, path: str = "/r.bin") -> bytes:
        return _get_range(self.srv, path, self.opt)

    def test_no_content_with_body_is_not_reused(self):
        """204'te sunucu govde gondermisse soket kayar: baglanti DONMEZ.

        Yeniden kullanilsaydi ikinci yanit, onceki 32 baytin ardinda
        baslardi (kayma); her istek taze baglanti acmali."""
        for _ in range(2):
            with httpcli.open_range(self.srv.url + "/nocontent", self.opt,
                                    0, END, timeout=5) as res:
                self.assertEqual(res.status, 204)
                self.assertEqual(res.read(), b"")
        self.assertEqual(self.srv.httpd.accepted, 2,
                         "204 yaniti havuza donmemeli")

    def test_unbounded_read_reuses_connection(self):
        """read() (sinirsiz) ile biten CL govdesi yeniden kullanilir."""
        for _ in range(3):
            self.assertEqual(len(self.get_range()), END + 1)
        self.assertEqual(self.srv.httpd.accepted, 1)

    def test_unbounded_read_after_partial_read_reuses(self):
        with httpcli.open_range(self.srv.url + "/r.bin", self.opt, 0, END,
                                timeout=5) as res:
            head = res.read(10)
            rest = res.read()              # kalan govde: sinirsiz okuma
            self.assertEqual(head + rest, BODY[:END + 1])
        self.assertEqual(len(self.get_range()), END + 1)
        self.assertEqual(self.srv.httpd.accepted, 1,
                         "kismi + sinirsiz okuma baglantiyi korumali")

    def test_chunked_unbounded_read_reuses(self):
        for _ in range(3):
            self.assertEqual(len(self.get_range("/chunked")), len(BODY))
        self.assertEqual(self.srv.httpd.accepted, 1,
                         "parcali (chunked) govde de havuza donmeli")

    def test_truncated_unbounded_read_raises_and_discards(self):
        with httpcli.open_range(self.srv.url + "/trunc.bin", self.opt, 0, END,
                                timeout=5) as res:
            with self.assertRaises(http.client.IncompleteRead):
                res.read()                 # eksik govde: sinirsiz okuma patlar
        self.assertEqual(len(self.get_range()), END + 1)
        self.assertEqual(self.srv.httpd.accepted, 2)


# ------------------------------------------------- yonlendirme semasi (H2)
class RedirectSchemeTests(unittest.TestCase):
    """302 hedefi http(s) disi olamaz: indirme baska tasimaya kacmaz."""

    def setUp(self):
        httpcli.close_pool()
        self.srv = _Server()
        self.addCleanup(self.srv.close)
        self.addCleanup(httpcli.close_pool)
        self.opt = httpcli.DlOptions(timeout=5)

    def redirect_to(self, target: str) -> None:
        self.srv.httpd.modes["/red.bin"] = "redirect"
        self.srv.httpd.redirect_to = target

    def test_local_file_target_rejected(self):
        secret = tmp_root(self, "ferdim-p113-gizli-") / "gizli.txt"
        secret.write_text("YEREL ICERIK", "utf-8")
        self.redirect_to(secret.as_uri())
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _get_range(self.srv, "/red.bin", self.opt)
        self.assertEqual(ctx.exception.code, 302)
        self.assertIn("semasi desteklenmiyor", str(ctx.exception.msg))
        ctx.exception.close()
        self.assertEqual(len(self.srv.httpd.requests), 1,
                         "yerel dosya hedefine ikinci istek yapilmamali")

    def test_data_and_ftp_targets_rejected(self):
        for target in ("data:text/plain,gizli", "ftp://127.0.0.1/x.bin"):
            self.redirect_to(target)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                _get_range(self.srv, "/red.bin", self.opt)
            self.assertEqual(ctx.exception.code, 302, target)
            self.assertIn("semasi desteklenmiyor", str(ctx.exception.msg),
                          target)
            ctx.exception.close()

    def test_legacy_redirect_handler_rejects_non_http_schemes(self):
        """Proxy/SOCKS yolu (urllib zinciri) ayni kapiyi uygular."""
        handler = httpcli.ScopedRedirectHandler()
        req = urllib.request.Request("http://a.test/x.bin",
                                     headers={"Authorization": "Basic eA=="})
        for target in ("file:///etc/hostname", "data:text/plain,x",
                       "ftp://127.0.0.1/x.bin"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                handler.redirect_request(req, None, 302, "Found", {}, target)
            self.assertEqual(ctx.exception.code, 302, target)
            self.assertIn("semasi desteklenmiyor", str(ctx.exception.msg),
                          target)
            ctx.exception.close()

    def test_redirect_scheme_rejected_on_proxy_path(self):
        """Gecitli (havuz disi) istek de 302 -> file: ile dosya okumaz."""
        proxy = _Proxy()
        self.addCleanup(proxy.close)
        for target in ("file:///etc/hostname", "data:text/plain,x",
                       "ftp://127.0.0.1/x.bin"):
            proxy.httpd.target = target
            opt = httpcli.DlOptions(proxy=proxy.url, timeout=5)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                httpcli.open_range("http://hedef.test/x.bin", opt, 0, END,
                                   timeout=5)
            self.assertEqual(ctx.exception.code, 302, target)
            ctx.exception.close()
        self.assertEqual(len(proxy.httpd.seen), 3,
                         "her hedef icin tek istek: zincir surmemeli")

    def test_opener_cannot_open_other_schemes(self):
        """Savunma katmani: opener'da file:/data:/ftp: isleyicisi yok."""
        handlers = httpcli._make_opener(self.opt, direct=True).handlers
        for klass in (urllib.request.FileHandler, urllib.request.FTPHandler,
                      urllib.request.DataHandler):
            self.assertNotIn(klass, [type(h) for h in handlers], klass)
        for url in ("file:///etc/hostname", "data:text/plain,x",
                    "ftp://127.0.0.1/x.bin"):
            with self.assertRaises(ValueError):
                httpcli._open_urllib(url, self.opt, {}, 5)

    def test_job_redirect_to_local_file_fails_without_writing(self):
        env = AppEnv(root=tmp_root(self, "ferdim-p113-jail-"))
        secret = tmp_root(self, "ferdim-p113-gizli-") / "gizli.txt"
        secret.write_text("YEREL ICERIK", "utf-8")
        self.redirect_to(secret.as_uri())
        cfg = env.load_config()
        cfg.update({"max_concurrent": 2, "retries": 0, "timeout": 2})
        job = DownloadJob(env, cfg,
                          JobSpec(url=self.srv.url + "/red.bin", segments=4))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            job.start()
            self.assertTrue(_wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"suren durum: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.ERROR, "is HATA'ya dusmeli")
        self.assertIn("302", job.error or "")
        self.assertFalse(os.path.exists(job.final_path),
                         f"yerel dosya indirilmis: {job.final_path}")
        self.assertEqual(os.listdir(os.path.dirname(job.final_path)), [],
                         "hedef klasore hicbir sey yazilmamali")


class _ProxyHandler(BaseHTTPRequestHandler):
    """Duz HTTP gecidi taklidi: her istege 302 + Location doner."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        with self.server.lock:
            self.server.seen.append(self.path)
        self.send_response(302)
        self.send_header("Content-Length", "0")
        self.send_header("Location", self.server.target)
        self.end_headers()


class _Proxy:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
        self.httpd.daemon_threads = True
        self.httpd.target = ""
        self.httpd.seen = []
        self.httpd.lock = threading.Lock()
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


# ------------------------------------------------------ Host basligi (L5)
class HostHeaderScopeTests(unittest.TestCase):
    """Kullanicinin Host basligi koken sinirini gecmez."""

    def setUp(self):
        httpcli.close_pool()
        self.srv = _Server()
        self.addCleanup(self.srv.close)
        self.addCleanup(httpcli.close_pool)

    def test_host_not_sent_to_another_origin(self):
        opt = httpcli.DlOptions(headers={"Host": "sanal.a.test"}, timeout=5,
                                origin=httpcli.origin_of("http://a.test/"))
        self.assertEqual(opt.headers_for("http://a.test/x")["Host"],
                         "sanal.a.test")
        self.assertNotIn("Host", opt.headers_for("http://b.test/x"),
                         "yabanci koken Host basligini almamali")

    def test_host_does_not_follow_cross_origin_redirect(self):
        other = _Server()
        self.addCleanup(other.close)
        self.srv.httpd.modes["/red.bin"] = "redirect"
        self.srv.httpd.redirect_to = other.url + "/r.bin"
        opt = httpcli.DlOptions(headers={"Host": "sanal.a.test"}, timeout=5)
        self.assertEqual(len(_get_range(self.srv, "/red.bin", opt)), END + 1)
        seen = other.httpd.seen[0]
        self.assertNotEqual(seen.get("host"), "sanal.a.test",
                            "Host basligi yabanci kokene tasindi")
        self.assertEqual(seen.get("host"), f"127.0.0.1:{other.port}")

    def test_host_kept_on_same_origin_redirect(self):
        self.srv.httpd.modes["/red.bin"] = "redirect"
        self.srv.httpd.redirect_to = self.srv.url + "/r.bin"
        opt = httpcli.DlOptions(headers={"Host": "sanal.a.test"}, timeout=5)
        self.assertEqual(len(_get_range(self.srv, "/red.bin", opt)), END + 1)
        self.assertEqual(self.srv.httpd.seen[-1].get("host"), "sanal.a.test",
                         "ayni koken kullanici Host'unu korur")

    def test_legacy_handler_drops_host_cross_origin(self):
        handler = httpcli.ScopedRedirectHandler()
        req = urllib.request.Request("http://a.test/x.bin",
                                     headers={"Host": "sanal.a.test",
                                              "Cookie": "a=b"})
        new = handler.redirect_request(req, None, 302, "Found", {},
                                       "http://b.test/x.bin")
        self.assertNotIn("Host", new.headers)
        self.assertNotIn("Cookie", new.headers)
        same = handler.redirect_request(req, None, 302, "Found", {},
                                        "http://a.test/y.bin")
        self.assertEqual(same.headers.get("Host"), "sanal.a.test")


# ------------------------------------------- havuz disi hop (L1) + fd (M1)
class PoolScopeTests(unittest.TestCase):
    def setUp(self):
        httpcli.close_pool()
        self.addCleanup(httpcli.close_pool)

    def test_redirect_hop_leaving_pool_falls_back_to_opener(self):
        """Gecit karari hedefe gore YENIDEN verilir (ortam gecidi)."""
        first = _Server()
        self.addCleanup(first.close)
        second = _Server()
        self.addCleanup(second.close)
        first.httpd.modes["/red.bin"] = "redirect"
        # farkli ana makine adi: _env_proxy yalniz ana makineyi gorur
        first.httpd.redirect_to = f"http://localhost:{second.port}/r.bin"
        with mock.patch.object(httpcli, "_env_proxy",
                               side_effect=lambda scheme, host:
                               host == "localhost"):
            for _ in range(2):
                with httpcli.open_range(first.url + "/red.bin",
                                        httpcli.DlOptions(timeout=5),
                                        0, END, timeout=5) as res:
                    self.assertEqual(res.status, 206)
                    self.assertNotIsInstance(
                        res, httpcli.PooledResponse,
                        "havuz disi hop urllib opener'inda bitmeli")
                    self.assertEqual(len(res.read()), END + 1)
        self.assertEqual(len(second.httpd.requests), 2,
                         "yonlendirme hedefi iki kez cagrilmali")
        self.assertEqual(second.httpd.accepted, 2,
                         "havuz disi hop baglanti yeniden kullanmamali")

    def test_pool_share_fits_the_fd_reserve(self):
        """Havuz, fd payinin (64) en fazla yarisini kullanir."""
        for soft, expect in ((64, 8), (128, 8), (256, 16), (1024, 32),
                             (4096, 32)):
            with mock.patch("resource.getrlimit", return_value=(soft, soft)):
                cap = httpcli._pool_max()
            self.assertEqual(cap, expect, f"soft={soft}")
            self.assertLessEqual(cap, httpcli.POOL_RESERVE // 2,
                                 "havuz fd payinin yarisini asmamali")
            links = max(1, soft - httpcli.POOL_RESERVE)
            self.assertLessEqual(links + cap, soft,
                                 f"soft={soft}: linkler + havuz siniri asiyor")
        pool = httpcli._pool()
        self.assertEqual(httpcli.pool_max(), pool.max)      # tek kaynak
        self.assertLessEqual(httpcli.pool_max(), httpcli.POOL_MAX)

    def test_job_budget_and_pool_agree(self):
        """Iki katman ayni butceyi paylasir: linkler + havuz <= soft."""
        with mock.patch.object(job_mod.resource, "getrlimit",
                               return_value=(256, 4096)):
            per_job = job_mod._fd_budget({"max_concurrent": 4})
        self.assertEqual(per_job, (256 - 64) // 4)
        with mock.patch("resource.getrlimit", return_value=(256, 4096)):
            cap = httpcli._pool_max()
        self.assertLessEqual(per_job * 4 + cap + 32, 256)


class DiscardBudgetTests(unittest.TestCase):
    """`_discard` tamponu bosaltirken bayt butcesini asar mi (L2)?"""

    class _Sock:
        def __init__(self, chunk: int, fail_after: int | None = None):
            self.chunk = chunk
            self.fail_after = fail_after
            self.calls = 0
            self.total = 0
            self.blocking = None

        def setblocking(self, flag):
            self.blocking = flag

        def recv(self, n):
            if self.fail_after is not None and self.calls >= self.fail_after:
                raise BlockingIOError(11, "EAGAIN")
            self.calls += 1
            size = min(n, self.chunk)
            self.total += size
            return b"x" * size

    class _Conn:
        def __init__(self, sock):
            self.sock = sock
            self.closed = False

        def close(self):
            self.closed = True

    def test_drain_is_capped(self):
        sock = self._Sock(chunk=65536)
        conn = self._Conn(sock)
        httpcli._discard(conn)
        self.assertTrue(conn.closed)
        self.assertIs(sock.blocking, False, "okuma non-blocking olmali")
        self.assertEqual(sock.total, httpcli._DRAIN_MAX)
        self.assertLessEqual(httpcli._DRAIN_MAX, 1 << 20)
        self.assertLessEqual(httpcli._DRAIN_MAX, 2 * httpcli.RCVBUF)

    def test_drain_stops_on_eagain(self):
        sock = self._Sock(chunk=1024, fail_after=2)
        conn = self._Conn(sock)
        httpcli._discard(conn)
        self.assertEqual(sock.calls, 2)
        self.assertEqual(sock.total, 2048)
        self.assertTrue(conn.closed)


if __name__ == "__main__":
    unittest.main()
