"""HTTP/3 (QUIC) tasimasi testleri.

Kapsam: Alt-Svc ayristirma/onbellek/son kullanma, mod secimi (off/auto/
force, proxy, bozuk kaynak), h3 hatasinda TCP'ye dusme, cekirdek
importunun aioquic'i yuklememesi, (aioquic + openssl varsa) 127.0.0.1
uzerinde gercek bir h3 sunucusuna karsi Range indirmesi ve origin basina
baglanti havuzu: es zamanli araliklar TEK QUIC baglantisinda ayri akislar,
sirali isteklerde yeniden kullanim, bos baglanti kapatma, baglanti
hatasinda havuz kaydinin dusmesi + TCP'ye dusme, yan dosyanin (fetch_small)
ve hls/dash/segment yolunun h3'ten gecmesi, close_all sonrasi is parcacigi
sizmamasi.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import email.message
import re
import shutil
import subprocess
import sys
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ferdim
from ferdim import paths
from ferdim.engine import h3, httpcli
from tests import helpers

ROOT = Path(__file__).resolve().parents[1]
OPENSSL = shutil.which("openssl")

try:                                    # yalnizca testin kendisi import eder
    import aioquic  # noqa: F401
    HAVE_AIOQUIC = True
except Exception:                       # pragma: no cover - ortamda yok
    HAVE_AIOQUIC = False

PAYLOAD = bytes(range(256)) * 16384     # 4 MiB (yavas okur regresyonu)


class _Clock:
    """h3 onbellek zamanini dondurmak icin sahte saat."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _message(**headers) -> email.message.Message:
    msg = email.message.Message()
    for key, val in headers.items():
        msg[key.replace("_", "-")] = val
    return msg


class _FakeResponse:
    """urllib yaniti taklidi (probe/open_range sozlesmesi)."""

    def __init__(self, status=206, total=4096, body=b"x"):
        self.status = status
        self.url = "https://ornek.test/x.bin"
        self.headers = _message(Content_Range=f"bytes 0-0/{total}",
                                ETag='"v1"', Content_Type="application/x-b")
        self.closed = False
        self._body = body

    def geturl(self):
        return self.url

    def read(self, n=-1):
        return self._body

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class AltSvcTest(unittest.TestCase):
    def setUp(self):
        h3.reset_cache()
        self.clock = _Clock()
        patcher = mock.patch.object(h3, "_now", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parse_port_and_max_age(self):
        self.assertEqual(h3.parse_alt_svc('h3=":443"; ma=60'),
                         ("", 443, 60.0))
        self.assertEqual(h3.parse_alt_svc('h3=":8443"'),
                         ("", 8443, h3.ALT_SVC_DEFAULT_MAX_AGE))

    def test_parse_picks_longest_and_ignores_other_protocols(self):
        value = 'h2=":443"; ma=10, h3=":443"; ma=30, h3=":443"; ma=90'
        self.assertEqual(h3.parse_alt_svc(value), ("", 443, 90.0))
        self.assertIsNone(h3.parse_alt_svc('h2=":443"; ma=60'))
        self.assertIsNone(h3.parse_alt_svc(""))
        self.assertIsNone(h3.parse_alt_svc(None))

    def test_parse_alt_authority_and_garbage(self):
        # duyurulan ana makine baglanti hedefidir (yetki/SNI kaynagin)
        self.assertEqual(h3.parse_alt_svc('h3="baska.test:8443"; ma=9'),
                         ("baska.test", 8443, 9.0))
        self.assertEqual(h3.parse_alt_svc('h3="[::1]:9000"; ma=9'),
                         ("::1", 9000, 9.0))
        self.assertIsNone(h3.parse_alt_svc('h3=":port"; ma=9'))
        self.assertIsNone(h3.parse_alt_svc("h3"))      # '=' yok
        self.assertIsNone(h3.parse_alt_svc('h3="443"'))  # iki nokta yok

    def test_cache_hit_and_expiry(self):
        url = "https://ornek.test/dosya.bin"
        self.assertTrue(h3.remember_alt_svc(url, 'h3=":8443"; ma=60'))
        self.assertEqual(h3.alt_svc_target(url), ("", 8443))
        self.clock.t += 59
        self.assertEqual(h3.alt_svc_target(url), ("", 8443))
        self.clock.t += 2
        self.assertIsNone(h3.alt_svc_target(url),
                          "suresi dolan kayit silinmeli")

    def test_cache_keeps_advertised_host(self):
        url = "https://ornek.test/dosya.bin"
        self.assertTrue(h3.remember_alt_svc(
            url, 'h3="cdn.ornek.test:9000"; ma=60'))
        self.assertEqual(h3.alt_svc_target(url), ("cdn.ornek.test", 9000))

    def test_ma_zero_clears(self):
        url = "https://ornek.test/dosya.bin"
        h3.remember_alt_svc(url, 'h3=":443"; ma=60')
        self.assertFalse(h3.remember_alt_svc(url, 'h3=":443"; ma=0'))
        self.assertIsNone(h3.alt_svc_target(url))

    def test_non_h3_header_not_cached(self):
        url = "https://ornek.test/dosya.bin"
        self.assertFalse(h3.remember_alt_svc(url, 'h2=":443"; ma=60'))
        self.assertIsNone(h3.alt_svc_target(url))

    def test_broken_backoff_expires(self):
        url = "https://ornek.test/dosya.bin"
        h3.mark_broken(url)
        self.assertTrue(h3.broken(url))
        self.clock.t += h3.BROKEN_SECS - 1
        self.assertTrue(h3.broken(url))
        self.clock.t += 2
        self.assertFalse(h3.broken(url), "10 dk sonra h3 yeniden denenmeli")

    def test_broken_is_per_origin(self):
        h3.mark_broken("https://ornek.test/a.bin")
        self.assertTrue(h3.broken("https://ornek.test/b.bin"))
        self.assertFalse(h3.broken("https://baska.test/a.bin"))

    def test_insecure_cache_is_separate(self):
        url = "https://ornek.test/dosya.bin"
        h3.remember_alt_svc(url, 'h3=":8443"; ma=60', insecure=True)
        self.assertIsNone(h3.alt_svc_target(url),
                          "guvenilmez isin kaydi guvenli ise sizmamali")
        self.assertEqual(h3.alt_svc_target(url, insecure=True), ("", 8443))

    def test_probe_remembers_only_the_final_origin(self):
        origin = "https://ilk.test/dosya.bin"
        final = "https://hedef.test/dosya.bin"
        fake = _FakeResponse(total=10)
        fake.url = final
        fake.headers = _message(Alt_Svc='h3=":8443"; ma=60')
        opt = httpcli.DlOptions(http3="off", timeout=3)
        with mock.patch.object(httpcli, "_open_tcp", return_value=fake):
            httpcli.probe(origin, opt)
        self.assertIsNone(h3.alt_svc_target(origin),
                          "yonlendirme hedefi kaynagin h3'unu belirleyemez")
        self.assertEqual(h3.alt_svc_target(final), ("", 8443))

    def test_probe_keeps_insecure_learning_separate(self):
        url = "https://ornek.test/dosya.bin"
        fake = _FakeResponse(total=10)
        fake.url = url
        fake.headers = _message(Alt_Svc='h3=":8443"; ma=60')
        opt = httpcli.DlOptions(http3="off", insecure=True, timeout=3)
        with mock.patch.object(httpcli, "_open_tcp", return_value=fake):
            httpcli.probe(url, opt)
        self.assertIsNone(h3.alt_svc_target(url))
        self.assertEqual(h3.alt_svc_target(url, insecure=True), ("", 8443))

    def test_cache_is_capped_and_keeps_recent(self):
        for i in range(h3.MAX_CACHE):
            self.assertTrue(h3.remember_alt_svc(
                f"https://h{i}.test/x", 'h3=":443"; ma=600'))
        self.assertEqual(len(h3._ALT_SVC), h3.MAX_CACHE)
        self.assertEqual(h3.alt_svc_target("https://h0.test/x"), ("", 443))
        h3.remember_alt_svc("https://yeni.test/x", 'h3=":443"; ma=600')
        self.assertEqual(len(h3._ALT_SVC), h3.MAX_CACHE)
        self.assertIsNotNone(h3.alt_svc_target("https://h0.test/x"),
                             "az kullanilan kayit son kullanma ile korunmali")
        self.assertIsNone(h3.alt_svc_target("https://h1.test/x"),
                          "en eski kayit atilmali")

    def test_broken_cache_is_capped(self):
        for i in range(h3.MAX_CACHE + 20):
            h3.mark_broken(f"https://h{i}.test/x")
        self.assertLessEqual(len(h3._BROKEN), h3.MAX_CACHE)


class ModeTest(unittest.TestCase):
    URL = "https://ornek.test/dosya.bin"

    def setUp(self):
        h3.reset_cache()
        patcher = mock.patch.object(h3, "available", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_off_never_uses_h3(self):
        h3.remember_alt_svc(self.URL, 'h3=":443"; ma=60')
        opt = httpcli.DlOptions(http3="off")
        self.assertFalse(h3.use_h3(self.URL, opt))
        self.assertEqual(h3.mode_of(opt), "off")

    def test_default_is_off(self):
        self.assertEqual(httpcli.DlOptions().http3, "off")

    def test_auto_needs_alt_svc(self):
        opt = httpcli.DlOptions(http3="auto")
        self.assertFalse(h3.use_h3(self.URL, opt), "Alt-Svc yok -> TCP")
        h3.remember_alt_svc(self.URL, 'h3=":443"; ma=60')
        self.assertTrue(h3.use_h3(self.URL, opt))

    def test_auto_disabled_by_proxy(self):
        h3.remember_alt_svc(self.URL, 'h3=":443"; ma=60')
        opt = httpcli.DlOptions(http3="auto", proxy="socks5h://127.0.0.1:1080")
        self.assertFalse(h3.use_h3(self.URL, opt))

    def test_auto_disabled_without_aioquic(self):
        h3.remember_alt_svc(self.URL, 'h3=":443"; ma=60')
        opt = httpcli.DlOptions(http3="auto")
        with mock.patch.object(h3, "available", return_value=False):
            self.assertFalse(h3.use_h3(self.URL, opt))

    def test_auto_disabled_for_http_scheme(self):
        url = "http://ornek.test/dosya.bin"
        h3.remember_alt_svc(url, 'h3=":443"; ma=60')
        self.assertFalse(h3.use_h3(url, httpcli.DlOptions(http3="auto")))

    def test_auto_skips_broken_origin(self):
        h3.remember_alt_svc(self.URL, 'h3=":443"; ma=60')
        opt = httpcli.DlOptions(http3="auto")
        h3.mark_broken(self.URL)
        self.assertFalse(h3.use_h3(self.URL, opt))

    def test_force_ignores_alt_svc(self):
        opt = httpcli.DlOptions(http3="force")
        self.assertTrue(h3.use_h3(self.URL, opt))
        h3.mark_broken(self.URL)          # force bozuk kaydi yok sayar
        self.assertTrue(h3.use_h3(self.URL, opt))

    def test_force_errors(self):
        opt = httpcli.DlOptions(http3="force")
        with self.assertRaises(h3.H3Unavailable):
            h3.use_h3("http://ornek.test/x", opt)
        with self.assertRaises(h3.H3Unavailable):
            h3.use_h3(self.URL, httpcli.DlOptions(
                http3="force", proxy="http://127.0.0.1:3128"))
        with mock.patch.object(h3, "available", return_value=False):
            with self.assertRaises(h3.H3Unavailable):
                h3.use_h3(self.URL, opt)

    def test_unknown_mode_falls_back_to_off(self):
        opt = httpcli.DlOptions(http3="evet")
        self.assertEqual(h3.mode_of(opt), "off")
        self.assertFalse(h3.use_h3(self.URL, opt))

    def test_aioquic_not_imported_by_core(self):
        """Cekirdek import zinciri aioquic'i yuklememeli."""
        code = ("import sys, ferdim.engine.httpcli; "
                "print(','.join(m for m in sys.modules "
                "if m == 'aioquic' or m.startswith('aioquic.')))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "",
                         "httpcli importu aioquic'i yuklememeli")

    def test_available_imports_lazily(self):
        code = ("import sys, ferdim.engine.h3 as h3; "
                "print('aioquic' in sys.modules, h3.available())")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        before, after = out.stdout.split()
        self.assertEqual(before, "False", "modul importu aioquic yuklememeli")
        self.assertEqual(after, str(HAVE_AIOQUIC))


class FallbackTest(unittest.TestCase):
    URL = "https://ornek.test/dosya.bin"

    def setUp(self):
        h3.reset_cache()
        patcher = mock.patch.object(h3, "available", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_auto_falls_back_to_tcp_on_h3_error(self):
        h3.remember_alt_svc(self.URL, 'h3=":443"; ma=60')
        opt = httpcli.DlOptions(http3="auto", timeout=3)
        fake = _FakeResponse(total=4321)
        with mock.patch.object(h3, "request",
                               side_effect=h3.H3Error("boom")), \
                mock.patch.object(httpcli, "_open_tcp",
                                  return_value=fake) as tcp:
            res = httpcli.probe(self.URL, opt)
        self.assertEqual(res.total, 4321)
        self.assertEqual(res.etag, '"v1"')
        self.assertEqual(tcp.call_count, 1, "h3 hatasinda TCP denenmeli")
        self.assertTrue(fake.closed)

    def test_auto_falls_back_for_range(self):
        h3.remember_alt_svc(self.URL, 'h3=":443"; ma=60')
        opt = httpcli.DlOptions(http3="auto", timeout=3)
        fake = _FakeResponse(body=b"veri")
        with mock.patch.object(h3, "request",
                               side_effect=ConnectionError("x")), \
                mock.patch.object(httpcli, "_open_tcp", return_value=fake):
            resp = httpcli.open_range(self.URL, opt, 10, 20)
        self.assertEqual(resp.read(4), b"veri")
        resp.close()
        self.assertTrue(fake.closed)

    def test_force_does_not_fall_back(self):
        opt = httpcli.DlOptions(http3="force", timeout=3)
        with mock.patch.object(h3, "request",
                               side_effect=h3.H3Error("boom")), \
                mock.patch.object(httpcli, "_open_tcp") as tcp:
            with self.assertRaises(h3.H3Error):
                httpcli.open_range(self.URL, opt, 0, 9)
        self.assertEqual(tcp.call_count, 0, "force modda TCP'ye dusulmez")

    def test_http_error_is_not_a_transport_failure(self):
        """Sunucunun 429 yaniti h3 calisiyor demek: TCP'ye dusulmez."""
        import urllib.error
        h3.remember_alt_svc(self.URL, 'h3=":443"; ma=60')
        opt = httpcli.DlOptions(http3="auto", timeout=3)
        err = urllib.error.HTTPError(self.URL, 429, "HTTP 429",
                                     _message(Retry_After="1"), None)
        try:
            with mock.patch.object(h3, "request", side_effect=err), \
                    mock.patch.object(httpcli, "_open_tcp") as tcp:
                with self.assertRaises(urllib.error.HTTPError):
                    httpcli.open_range(self.URL, opt, 0, 9)
        finally:
            err.close()
        self.assertEqual(tcp.call_count, 0)

    def test_session_failure_marks_origin_broken(self):
        session = h3._Session(self.URL, httpcli.DlOptions(http3="auto"),
                              {}, 1)
        session._fail(ConnectionError("baglanti koptu"))
        self.assertTrue(h3.broken(self.URL))

    def test_timeout_raises_builtin_timeouterror(self):
        """Okuma zaman asimi job.py'nin stall dalini tetiklemeli."""
        session = h3._Session(self.URL, httpcli.DlOptions(http3="auto"),
                              {}, 0.05)
        session._loop = mock.Mock()
        session._loop.is_closed.return_value = False
        session._queue = mock.Mock()
        waiter = concurrent.futures.Future()
        waiter.set_exception(concurrent.futures.TimeoutError())
        with mock.patch.object(h3.asyncio, "run_coroutine_threadsafe",
                               return_value=waiter):
            with self.assertRaises(TimeoutError):
                session.read(10)
        self.assertTrue(h3.broken(self.URL))


class AvailabilityTest(unittest.TestCase):
    def setUp(self):
        self._old = h3._AVAILABLE
        self.addCleanup(setattr, h3, "_AVAILABLE", self._old)

    def test_missing_aioquic_is_cached(self):
        """aioquic yoksa import her cagrida yeniden denenmemeli."""
        h3._AVAILABLE = None
        calls = []

        def boom():
            calls.append(1)
            raise ImportError("aioquic yok")

        with mock.patch.object(h3, "_load", boom):
            self.assertFalse(h3.available())
            self.assertFalse(h3.available())
        self.assertEqual(len(calls), 1, "negatif sonuc onbelleklenmeli")


class SessionTest(unittest.TestCase):
    URL = "https://ornek.test/dosya.bin"

    def test_connect_timeout_is_bounded(self):
        """El sikismasi, stall suresinden bagimsiz ve kisa olmali."""
        opt = httpcli.DlOptions(http3="auto")
        self.assertEqual(h3._Session(self.URL, opt, {}, 30)._connect_timeout,
                         h3.CONNECT_SECS)
        self.assertEqual(h3._Session(self.URL, opt, {}, 2)._connect_timeout, 2)

    def test_target_defaults_to_origin(self):
        session = h3._Session(self.URL, httpcli.DlOptions(http3="auto"),
                              {}, 5)
        self.assertIsNone(session._target)


class StartRaceTest(unittest.TestCase):
    """start() zaman asimi: istek iptal edilmeli, akis slotu sizmamali."""

    URL = "https://ornek.test/dosya.bin"

    def setUp(self):
        h3.reset_cache()
        self.addCleanup(h3.close_all)

    def test_handshake_timeout_cancels_and_marks_broken(self):
        """El sikismasi zaman asimi: istek iptal, kaynak bozuk, akis yok.

        Python 3.9/3.10'da `concurrent.futures.TimeoutError` builtin
        TimeoutError'dan ayri bir siniftir; yanlis sinif yakalanirsa istek
        iptal edilmez ve sonradan akis kaydedip slot sizdirirdi."""
        started = threading.Event()
        cancelled = threading.Event()

        async def never(url, opt, target):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        opt = httpcli.DlOptions(http3="force", timeout=0.2)
        session = h3._Session(self.URL, opt, {}, 0.2)
        with mock.patch.object(h3, "_conn_get", never):
            with self.assertRaises(TimeoutError):
                session.start()
        self.assertTrue(started.is_set(), "istek baslamali")
        self.assertTrue(cancelled.wait(5),
                        "zaman asiminda istek iptal edilmeli")
        self.assertTrue(h3.broken(self.URL), "zaman asimi kaynagi bozmali")
        self.assertTrue(session._stream.reader_done, "akis birakilmali")
        self.assertEqual(h3.pool_stats()["streams"], 0)

    def test_released_stream_is_not_registered(self):
        """Birakildiktan SONRA tamamlanan istek akis kaydetmemeli."""
        conn = mock.Mock()
        conn.streams = {}
        conn.closed = False
        conn.paused = False
        session = h3._Session(self.URL, httpcli.DlOptions(http3="auto"), {}, 5)
        session._stream = h3._Stream()

        async def late(url, opt, target):
            # start() zaman asimina ugramis gibi: akis birakilmis donuyor
            session._stream.reader_done = True
            return conn

        loop = h3._loop()
        with mock.patch.object(h3, "_conn_get", late):
            fut = asyncio.run_coroutine_threadsafe(session._open(), loop)
            with self.assertRaises(h3.H3Error):
                fut.result(5)
        self.assertEqual(conn.streams, {}, "birakilmis akis kaydedilmemeli")
        self.assertEqual(conn.proto.h3.send_headers.call_count, 0,
                         "birakilmis akisa istek gonderilmemeli")


class ConfigTest(unittest.TestCase):
    def test_default_off(self):
        self.assertEqual(paths.DEFAULTS["http3"], "off")

    def test_sanitize_keeps_valid_and_resets_invalid(self):
        from ferdim import api
        for value in ("off", "auto", "force"):
            cfg = dict(paths.DEFAULTS)
            cfg["http3"] = value
            api._sanitize_config(cfg)
            self.assertEqual(cfg["http3"], value)
        for value in ("evet", "", 1, None):
            cfg = dict(paths.DEFAULTS)
            cfg["http3"] = value
            api._sanitize_config(cfg)
            self.assertEqual(cfg["http3"], "off")


class PackagingTest(unittest.TestCase):
    def test_pyproject_version_matches_package(self):
        try:
            import tomllib
        except ImportError:               # pragma: no cover - python < 3.11
            self.skipTest("tomllib yok")
        data = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
        init = (ROOT / "ferdim" / "__init__.py").read_text("utf-8")
        found = re.search(r'^__version__ = "([^"]+)"', init, re.M)
        self.assertIsNotNone(found, "__version__ bulunamadi")
        self.assertEqual(data["project"]["version"], found.group(1),
                         "pyproject.toml surumu ferdim/__init__.py ile ayni "
                         "olmali")
        self.assertEqual(data["project"]["version"], ferdim.__version__)
        self.assertEqual(data["project"]["optional-dependencies"]["h3"],
                         ["aioquic>=1.0"])

    def test_no_runtime_dependencies(self):
        try:
            import tomllib
        except ImportError:               # pragma: no cover - python < 3.11
            self.skipTest("tomllib yok")
        data = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
        self.assertFalse(data["project"].get("dependencies"),
                         "cekirdek bagimlilik eklememeli (stdlib kurali)")


class _QuicTestServer:
    """127.0.0.1 uzerinde tek is parcacikli aioquic HTTP/3 sunucusu.

    Sunucu tarafi QUIC baglantisi ve istek sayisini tutar: istemcinin
    baglanti yeniden kullanimi (coklama) boylece dogrulanir."""

    TEXT = b"h3 yan dosya\n"

    def __init__(self, cert: Path, key: Path):
        self._cert, self._key = cert, key
        self._ready = threading.Event()
        self._stop = None
        self._loop = None
        self._thread = None
        self._lock = threading.Lock()
        self._protos: list = []
        self.port = 0
        # istatistik: kabul edilen QUIC baglantisi / istek akisi
        self.connections = 0
        self.requests = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ferdim-h3-test-server")
        self._thread.start()
        if not self._ready.wait(15):
            raise RuntimeError("h3 test sunucusu baslamadi")

    def close(self) -> None:
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(stop.set)
        if self._thread is not None:
            self._thread.join(5)

    def kill_connections(self) -> None:
        """Sunucu tarafi tum QUIC baglantilarini kapatir (hata benzetimi)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        def kill():
            with self._lock:
                protos = list(self._protos)
                self._protos.clear()
            for proto in protos:
                proto.close()
        loop.call_soon_threadsafe(kill)

    def _run(self) -> None:
        from aioquic.asyncio import QuicConnectionProtocol, serve
        from aioquic.h3.connection import H3_ALPN, H3Connection
        from aioquic.h3.events import HeadersReceived
        from aioquic.quic.configuration import QuicConfiguration

        server = self

        class Proto(QuicConnectionProtocol):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.h3c = H3Connection(self._quic)
                with server._lock:
                    server.connections += 1
                    server._protos.append(self)

            def quic_event_received(self, event):
                for ev in self.h3c.handle_event(event):
                    if isinstance(ev, HeadersReceived):
                        self._answer(ev)

            def _answer(self, ev):
                headers = dict(ev.headers)
                path = headers.get(b":path", b"/")
                with server._lock:
                    server.requests += 1
                if path.startswith(b"/hata"):
                    # yanit vermeden baglantiyi kapat: istemci akisi H3Error
                    # almali, havuz kaydi dusmeli
                    self.close()
                    self.transmit()
                    return
                if path.startswith(b"/metin"):
                    body = server.TEXT
                    out = [(b":status", b"200"),
                           (b"content-type", b"text/plain"),
                           (b"content-length", str(len(body)).encode())]
                    self.h3c.send_headers(ev.stream_id, out)
                    self.h3c.send_data(ev.stream_id, body, end_stream=True)
                    self.transmit()
                    return
                rng = headers.get(b"range", b"")
                start, end = 0, len(PAYLOAD) - 1
                m = re.match(rb"bytes=(\d+)-(\d*)", rng or b"")
                if m:
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), end)
                body = PAYLOAD[start:end + 1]
                out = [(b":status", b"206" if m else b"200"),
                       (b"content-type", b"application/octet-stream"),
                       (b"content-length", str(len(body)).encode()),
                       (b"accept-ranges", b"bytes"),
                       (b"etag", b'"h3"')]
                if m:
                    span = f"bytes {start}-{end}/{len(PAYLOAD)}"
                    out.append((b"content-range", span.encode()))
                self.h3c.send_headers(ev.stream_id, out)
                self.h3c.send_data(ev.stream_id, body, end_stream=True)
                self.transmit()

        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        stop = asyncio.Event()
        self._stop = stop

        async def main():
            conf = QuicConfiguration(is_client=False,
                                     alpn_protocols=list(H3_ALPN))
            conf.load_cert_chain(str(self._cert), str(self._key))
            srv = await serve("127.0.0.1", 0, configuration=conf,
                              create_protocol=Proto)
            self.port = srv._transport.get_extra_info("sockname")[1]
            self._ready.set()
            await stop.wait()
            srv.close()

        try:
            loop.run_until_complete(main())
        finally:
            loop.close()


@unittest.skipUnless(HAVE_AIOQUIC, "aioquic kurulu degil")
@unittest.skipUnless(OPENSSL, "openssl komutu yok")
class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = helpers.tmp_root(cls, "ferdim-h3-")
        cls.cert = cls.root / "cert.pem"
        cls.key = cls.root / "key.pem"
        subprocess.run(
            [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(cls.key), "-out", str(cls.cert), "-days", "1",
             "-subj", "/CN=localhost",
             "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"],
            check=True, capture_output=True)
        cls.server = _QuicTestServer(cls.cert, cls.key)
        cls.server.start()
        cls.url = f"https://127.0.0.1:{cls.server.port}/dosya.bin"

    @classmethod
    def tearDownClass(cls):
        h3.close_all()
        cls.server.close()

    def setUp(self):
        h3.reset_cache()

    def test_range_over_http3(self):
        opt = httpcli.DlOptions(http3="force", insecure=True, timeout=10)
        p = httpcli.probe(self.url, opt)
        self.assertEqual(p.status, 206, p)
        self.assertEqual(p.total, len(PAYLOAD))
        self.assertTrue(p.accept_ranges)
        self.assertEqual(p.etag, '"h3"')
        with httpcli.open_range(self.url, opt, 100, 1123, timeout=10) as res:
            self.assertEqual(res.status, 206)
            self.assertEqual(res.headers.get("Content-Range"),
                             f"bytes 100-1123/{len(PAYLOAD)}")
            chunks = []
            while True:                        # job.py gibi parca parca oku
                chunk = res.read(250)
                if not chunk:
                    break
                chunks.append(chunk)
        self.assertEqual(b"".join(chunks), PAYLOAD[100:1124])
        self.assertFalse(h3.broken(self.url),
                         "basarili h3 kaynagi bozulmamali")

    def test_auto_uses_advertised_port(self):
        """Alt-Svc `h3=\":port\"` ile duyurulan port gercekten kullanilmali.

        URL'in kendi portu (9) kapali: basari yalnizca duyurulan porta
        baglanildiginda mumkun."""
        url = "https://127.0.0.1:9/dosya.bin"
        h3.remember_alt_svc(url, f'h3=":{self.server.port}"; ma=60',
                            insecure=True)
        opt = httpcli.DlOptions(http3="auto", insecure=True, timeout=10)
        p = httpcli.probe(url, opt)
        self.assertEqual(p.status, 206, p)
        self.assertEqual(p.total, len(PAYLOAD))
        with httpcli.open_range(url, opt, 0, 99, timeout=10) as res:
            self.assertEqual(res.headers.get("Content-Range"),
                             f"bytes 0-99/{len(PAYLOAD)}")
            self.assertEqual(res.read(100), PAYLOAD[:100])

    def test_auto_uses_advertised_host(self):
        """Duyurulan ana makine de baglanti hedefi olmali.

        URL'in ana makinesi (localhost:9) kapali: basari yalnizca
        duyurulan 127.0.0.1:<port> hedefine baglanildiginda mumkun."""
        url = "https://localhost:9/dosya.bin"
        h3.remember_alt_svc(
            url, f'h3="127.0.0.1:{self.server.port}"; ma=60',
            insecure=True)
        opt = httpcli.DlOptions(http3="auto", insecure=True, timeout=10)
        p = httpcli.probe(url, opt)
        self.assertEqual(p.total, len(PAYLOAD))

    def test_tail_survives_stream_end(self):
        """Akis bittikten sonra okur gec kalirsa kuyruk kaybolmamali.

        Kucuk aralik kuyruga sigar: sunucu tarafi hemen biter, olay
        dongusu kapanir. Eski surum donguyu bu noktada kapatip kuyruktaki
        kuyrugu H3Error olarak kaybediyordu (okur 0,3 sn bekliyor)."""
        opt = httpcli.DlOptions(http3="force", insecure=True, timeout=10)
        with httpcli.open_range(self.url, opt, 0, 2047, timeout=10) as res:
            time.sleep(0.3)              # dongu kapanmadan okumaya baslama
            self.assertEqual(res.read(4096), PAYLOAD[:2048])

    def test_slow_reader_gets_whole_body(self):
        """Akis bittikten sonra kuyrukta kalan parcalar kaybolmamali.

        Yavas okur + 4 MiB tek aralik: okur kuyrugu bosalmadan akis
        biterse son parcalar kaybolmamali."""
        opt = httpcli.DlOptions(http3="force", insecure=True, timeout=10)
        end = len(PAYLOAD) - 1
        got = bytearray()
        with httpcli.open_range(self.url, opt, 0, end, timeout=10) as res:
            while True:
                chunk = res.read(65536)
                if not chunk:
                    break
                got += chunk
                time.sleep(0.002)          # kuyrugu doldur, sonra bosalt
        self.assertEqual(bytes(got), PAYLOAD)

    def test_force_without_aioquic_errors(self):
        opt = httpcli.DlOptions(http3="force", insecure=True, timeout=10)
        with mock.patch.object(h3, "available", return_value=False):
            with self.assertRaises(h3.H3Unavailable):
                httpcli.open_range(self.url, opt, 0, 9)

    def test_self_signed_rejected_by_default(self):
        """TLS dogrulamasi varsayilan acik: kendinden imzali sertifika
        reddedilir ve kaynak bozuk isaretlenir."""
        opt = httpcli.DlOptions(http3="force", timeout=8)
        with warnings.catch_warnings():
            # aioquic el sikismasi reddedilince tasimayi kapatmiyor
            warnings.simplefilter("ignore", ResourceWarning)
            with self.assertRaises(Exception):
                with httpcli.open_range(self.url, opt, 0, 9, timeout=8) as res:
                    res.read(16)
        self.assertTrue(h3.broken(self.url))


@unittest.skipUnless(HAVE_AIOQUIC, "aioquic kurulu degil")
@unittest.skipUnless(OPENSSL, "openssl komutu yok")
class PoolTest(unittest.TestCase):
    """Origin basina baglanti havuzu: coklama, yeniden kullanim, kapanis."""

    @classmethod
    def setUpClass(cls):
        cls.root = helpers.tmp_root(cls, "ferdim-h3-pool-")
        cls.cert = cls.root / "cert.pem"
        cls.key = cls.root / "key.pem"
        subprocess.run(
            [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(cls.key), "-out", str(cls.cert), "-days", "1",
             "-subj", "/CN=localhost",
             "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"],
            check=True, capture_output=True)
        cls.server = _QuicTestServer(cls.cert, cls.key)
        cls.server.start()
        base = f"https://127.0.0.1:{cls.server.port}"
        cls.url = base + "/dosya.bin"
        cls.text_url = base + "/metin.txt"

    @classmethod
    def tearDownClass(cls):
        h3.close_all()
        cls.server.close()

    def setUp(self):
        h3.close_all()                # her test temiz havuzla baslasin
        h3.reset_cache()
        self.base_conns = self.server.connections

    def tearDown(self):
        h3.close_all()

    def opt(self, mode: str = "force", timeout: float = 10) -> httpcli.DlOptions:
        return httpcli.DlOptions(http3=mode, insecure=True, timeout=timeout)

    def _read_all(self, res) -> bytes:
        buf = bytearray()
        while True:
            chunk = res.read(65536)
            if not chunk:
                return bytes(buf)
            buf += chunk

    def _wait(self, pred, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return False

    def _burst(self, n: int, fn) -> list[BaseException]:
        """n is parcacigi ayni anda: fn(i) baglanti/akis acar, bariyerde
        bekler (hepsi kayitli olsun), sonra devam eder."""
        gate = threading.Barrier(n, timeout=20)
        errors: list[BaseException] = []
        lock = threading.Lock()

        def work(i):
            try:
                fn(i, gate)
            except BaseException as ex:      # noqa: BLE001 - testte raporlanir
                with lock:
                    errors.append(ex)
                try:
                    gate.abort()
                except Exception:
                    pass

        threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        return errors

    def test_concurrent_ranges_use_one_connection(self):
        """Es zamanli N aralik TEK QUIC baglantisinda ayri akislar olmali."""
        n = 8
        span = len(PAYLOAD) // n
        got: dict[int, bytes] = {}

        def work(i, gate):
            start = i * span
            with httpcli.open_range(self.url, self.opt(), start,
                                    start + span - 1, timeout=15) as res:
                gate.wait()
                got[i] = self._read_all(res)

        errors = self._burst(n, work)
        self.assertEqual(errors, [])
        self.assertEqual(self.server.connections - self.base_conns, 1,
                         "es zamanli araliklar tek QUIC baglantisi kullanmali")
        for i in range(n):
            self.assertEqual(got.get(i), PAYLOAD[i * span:(i + 1) * span],
                             f"aralik {i} bozuk")
        self.assertEqual(h3.pool_stats()["connections"], 1,
                         "baglanti sonraki istekler icin havuzda kalmali")

    def test_sequential_requests_reuse_connection(self):
        for _ in range(3):
            with httpcli.open_range(self.url, self.opt(), 0, 99,
                                    timeout=10) as res:
                self.assertEqual(res.read(100), PAYLOAD[:100])
        self.assertEqual(self.server.connections - self.base_conns, 1,
                         "sirali istekler ayni baglantiyi kullanmali")

    def test_stream_cap_opens_second_connection(self):
        """MAX_STREAMS asilinca ayni hedef icin ikinci baglanti acilir."""
        n = 4
        got: dict[int, bytes] = {}

        def work(i, gate):
            with httpcli.open_range(self.url, self.opt(), 0, 99,
                                    timeout=15) as res:
                gate.wait()
                got[i] = res.read(100)

        with mock.patch.object(h3, "MAX_STREAMS", 2):
            errors = self._burst(n, work)
        self.assertEqual(errors, [])
        self.assertEqual(len(got), n)
        self.assertEqual(self.server.connections - self.base_conns, 2,
                         "akis siniri asilinca ikinci baglanti acilmali")

    def test_idle_connection_closes(self):
        with httpcli.open_range(self.url, self.opt(), 0, 99,
                                timeout=10) as res:
            res.read(100)
        self.assertEqual(h3.pool_stats()["connections"], 1)
        with mock.patch.object(h3, "POOL_IDLE_SECS", 0.2), \
                mock.patch.object(h3, "POOL_SWEEP_SECS", 0.05):
            self.assertTrue(
                self._wait(lambda: h3.pool_stats()["connections"] == 0),
                "bos baglanti bosta kapatilmali")
        self.assertFalse(h3.broken(self.url),
                         "bosta kapanma kaynagi bozmamali")
        with httpcli.open_range(self.url, self.opt(), 0, 9, timeout=10) as res:
            self.assertEqual(res.read(10), PAYLOAD[:10])
        self.assertEqual(self.server.connections - self.base_conns, 2,
                         "kapanan baglanti yerine yenisi kurulmali")

    def test_server_close_does_not_break_origin(self):
        """Sunucu bos baglantiyi temiz kapatirsa kaynak bozulmamali."""
        with httpcli.open_range(self.url, self.opt(), 0, 9,
                                timeout=10) as res:
            self.assertEqual(res.read(10), PAYLOAD[:10])
        self.server.kill_connections()
        self.assertTrue(
            self._wait(lambda: h3.pool_stats()["connections"] == 0),
            "kapanan baglanti havuzdan dusmeli")
        self.assertFalse(h3.broken(self.url),
                         "akis yokken kapanma kaynagi bozmamali")
        with httpcli.open_range(self.url, self.opt(), 0, 9, timeout=10) as res:
            self.assertEqual(res.read(10), PAYLOAD[:10])

    def test_connection_loss_evicts_and_fails_stream(self):
        """Baglanti kopunca havuz kaydi duser, akis H3Error alir."""
        with httpcli.open_range(self.url, self.opt(), 0, 9,
                                timeout=10) as res:
            self.assertEqual(res.read(10), PAYLOAD[:10])
        self.assertEqual(h3.pool_stats()["connections"], 1)
        bad = self.url.rsplit("/", 1)[0] + "/hata.bin"
        with self.assertRaises(h3.H3Error):
            with httpcli.open_range(bad, self.opt(), 0, 9, timeout=10) as res:
                res.read(10)
        self.assertEqual(h3.pool_stats()["connections"], 0,
                         "kopan baglanti havuzdan dusmeli")
        self.assertTrue(h3.broken(bad), "kopan kaynak bozuk isaretlenmeli")

    def test_auto_falls_back_to_tcp_after_h3_loss(self):
        with httpcli.open_range(self.url, self.opt(), 0, 9,
                                timeout=10) as res:
            res.read(10)
        bad = self.url.rsplit("/", 1)[0] + "/hata.bin"
        with self.assertRaises(h3.H3Error):
            with httpcli.open_range(bad, self.opt(), 0, 9, timeout=10) as res:
                res.read(10)
        h3.remember_alt_svc(bad, f'h3=":{self.server.port}"; ma=60',
                            insecure=True)
        fake = _FakeResponse(total=1234)
        with mock.patch.object(httpcli, "_open_tcp",
                               return_value=fake) as tcp:
            probe = httpcli.probe(bad, self.opt("auto", 5))
        self.assertEqual(probe.total, 1234)
        self.assertEqual(tcp.call_count, 1,
                         "bozuk kaynakta auto mod TCP'ye dusmeli")

    def test_fetch_small_over_http3(self):
        """Yan dosya (saglama/manifest) ayni havuzdan h3 ile cekilmeli."""
        before = self.server.requests
        text = httpcli.fetch_small(self.text_url, self.opt())
        self.assertEqual(text, _QuicTestServer.TEXT.decode())
        self.assertEqual(self.server.requests - before, 1)
        self.assertEqual(h3.pool_stats()["connections"], 1)
        self.assertFalse(h3.broken(self.text_url))

    def test_opener_path_uses_h3(self):
        """hls.fetch_text / dash.fetch_mpd / segments.fetch_all ayni
        `_make_opener(opt).open(request_of(...))` imzasini kullanir; h3
        acikken istek h3'ten gecmeli."""
        opt = self.opt()
        opener = httpcli._make_opener(opt)
        with opener.open(httpcli.request_of(self.text_url, opt),
                         timeout=10) as res:
            self.assertIsInstance(res, h3.Response)
            self.assertEqual(res.status, 200)
            self.assertEqual(res.read(), _QuicTestServer.TEXT)
        self.assertFalse(h3.broken(self.text_url))

    def test_close_all_releases_thread(self):
        with httpcli.open_range(self.url, self.opt(), 0, 9,
                                timeout=10) as res:
            self.assertEqual(res.read(10), PAYLOAD[:10])
        self.assertIn("ferdim-h3-loop",
                      [t.name for t in threading.enumerate()])
        h3.close_all()
        self.assertNotIn("ferdim-h3-loop",
                         [t.name for t in threading.enumerate()],
                         "close_all sonrasi is parcacigi kalmamali")
        self.assertEqual(h3.pool_stats()["connections"], 0)
        self.assertEqual(h3.pool_stats()["streams"], 0)
        with httpcli.open_range(self.url, self.opt(), 0, 9,
                                timeout=10) as res:      # yeniden kurulabilir
            self.assertEqual(res.read(10), PAYLOAD[:10])
        self.assertIn("ferdim-h3-loop",
                      [t.name for t in threading.enumerate()])

    def test_slow_reader_keeps_buffer_bounded(self):
        """Yavas okur: okunmamis tampon siniri asmamali, veri bayt-tam.

        aioquic akis kredisini alinan bayta gore otomatik artirir; geri
        basinc UDP okumasini duraklatarak uygulanir (STREAM_BUFFER)."""
        bound = 128 * 1024
        opt = self.opt()
        got = bytearray()
        with mock.patch.object(h3, "STREAM_BUFFER", bound):
            end = len(PAYLOAD) - 1
            with httpcli.open_range(self.url, opt, 0, end,
                                    timeout=30) as res:
                while True:
                    chunk = res.read(65536)
                    if not chunk:
                        break
                    got += chunk
                    time.sleep(0.004)      # okuru agdan yavas birak
        self.assertEqual(bytes(got), PAYLOAD, "veri bayt-tam olmali")
        peak = h3.pool_stats()["peak_buffered"]
        self.assertLessEqual(peak, bound * 3,
                             f"tampon sinirsiz buyudu: {peak} bayt")


if __name__ == "__main__":
    unittest.main()
