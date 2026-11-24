"""Kimlik basliklari koken sinirini gecmez (yonlendirme + ayna).

Kural: Authorization, Cookie ve kullanicinin ozel basliklari YALNIZCA
kimlik sahibi kokenine (sema + ana makine + port) gider. Farkli kokenli
30x hedefi (https->http dususu dahil) ve farkli kokenli ayna bunlari
GORMEZ; ayni kokenli hedef/ayna kimligi korur.

Kimlik OLMAYANLAR her atlamada kalir: Proxy-Authorization (gecidin
kimligi), Referer (CDN hotlink denetimi) ve If-Range (dogrulayici;
yonlendirme hedefi dosyayi sunan kaynak oldugu icin devam dogrulamasi
orada gerekir).

Ag testleri yerel is parcacikli sunucular kullanir (internet yok, uyku
yok); sunucular istegin basliklarini kaydeder.
"""
from __future__ import annotations

import hashlib
import sys
import threading
import time
import unittest
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim.engine import h3, httpcli
from ferdim.engine.job import DownloadJob, JobSpec, JobState
from ferdim.paths import AppEnv
from tests.helpers import DATA, DATA_SHA, tmp_root


def wait_state(job, states, timeout=60):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if job.state in states:
            return True
        time.sleep(0.02)
    return False


class _Handler(BaseHTTPRequestHandler):
    """Yonlendirme + Range sunan, gelen basliklari kaydeden isleyici."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _reply(self, code, body, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        for key, val in extra:
            self.send_header(key, val)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        srv = self.server
        path = urlsplit(self.path).path
        with srv.lock:
            srv.seen.append((path, {k.lower(): v
                                    for k, v in self.headers.items()}))
        target = srv.redirects.get(path)
        if target is not None:
            self._reply(302, b"", [("Location", target)])
            return
        body = srv.files.get(path)
        if body is None:              # yan dosya yok: 404 (yan dosya arayisi)
            self._reply(404, b"yok")
            return
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            first, _, last = rng[6:].partition("-")
            a = int(first)
            b = min(int(last), len(body) - 1) if last else len(body) - 1
            self._reply(206, body[a:b + 1], [
                ("Content-Range", f"bytes {a}-{b}/{len(body)}"),
                ("Accept-Ranges", "bytes"), ("ETag", '"sunucu"')])
            return
        self._reply(200, body, [("Accept-Ranges", "bytes"),
                                ("ETag", '"sunucu"')])


class RecordingServer:
    """Yerel sunucu: files (yol -> govde), redirects (yol -> mutlak URL)."""

    def __init__(self, files=None, redirects=None):
        ThreadingHTTPServer.request_queue_size = 128
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.files = dict(files or {})
        self.server.redirects = dict(redirects or {})
        self.server.seen = []
        self.server.lock = threading.Lock()
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def headers(self, path=None):
        """Kaydedilen baslik sozlukleri (yol verilirse o yol icin)."""
        with self.server.lock:
            recs = list(self.server.seen)
        return [h for p, h in recs if path is None or p == path]


def _creds(opt_url: str, **kw) -> httpcli.DlOptions:
    """Koken kapsami kurulmus secenekler + kimlik basliklari."""
    headers = {"X-Ferdim-Ozel": "gizli"}
    headers.update(kw.pop("headers", {}))
    return httpcli.DlOptions(
        user="kullanici", password="parola", cookies="oturum=abc",
        headers=headers, origin=httpcli.origin_of(opt_url), **kw)


class OriginTests(unittest.TestCase):
    def test_normalises_scheme_host_and_default_port(self):
        self.assertEqual(httpcli.origin_of("HTTPS://Ornek.TEST/x"),
                         ("https", "ornek.test", 443))
        self.assertEqual(httpcli.origin_of("http://ornek.test:80/x"),
                         ("http", "ornek.test", 80))
        self.assertNotEqual(httpcli.origin_of("http://ornek.test/x"),
                            httpcli.origin_of("http://ornek.test:8080/x"))

    def test_different_port_is_a_different_origin(self):
        a = httpcli.origin_of("http://127.0.0.1:1000/a")
        self.assertTrue(httpcli.same_origin(a, "http://127.0.0.1:1000/b"))
        self.assertFalse(httpcli.same_origin(a, "http://127.0.0.1:1001/b"))

    def test_https_to_http_downgrade_is_a_different_origin(self):
        a = httpcli.origin_of("https://ornek.test/gizli")
        self.assertFalse(httpcli.same_origin(a, "http://ornek.test/gizli"))

    def test_unparsable_origin_is_never_same(self):
        self.assertFalse(httpcli.same_origin(("", "", 0), "http://a/x"))
        self.assertFalse(httpcli.same_origin(("http", "a", 80), ""))
        self.assertFalse(httpcli.same_origin(None, "http://a/x"))

    def test_subdomain_is_another_origin(self):
        a = httpcli.origin_of("http://a.ornek.test/x")
        self.assertFalse(httpcli.same_origin(a, "http://b.ornek.test/x"))


class HeadersForTests(unittest.TestCase):
    def test_same_origin_keeps_credentials(self):
        opt = _creds("http://a.test/basla")
        h = opt.headers_for("http://a.test/parca")
        self.assertIn("Authorization", h)
        self.assertEqual(h["Cookie"], "oturum=abc")
        self.assertEqual(h["X-Ferdim-Ozel"], "gizli")

    def test_other_origin_drops_credentials(self):
        opt = _creds("http://a.test/basla")
        h = opt.headers_for("http://b.test/parca")
        self.assertNotIn("Authorization", h)
        self.assertNotIn("Cookie", h)
        self.assertNotIn("X-Ferdim-Ozel", h)
        # protokol basliklari kalir: Range dogrulugu ve kimliksiz indirme
        self.assertEqual(h["Accept-Encoding"], "identity")
        self.assertEqual(h["User-Agent"], "")

    def test_scheme_change_drops_credentials(self):
        opt = _creds("https://a.test/basla")
        for url in ("http://a.test/parca", "http://a.test:443/parca"):
            h = opt.headers_for(url)
            self.assertNotIn("Authorization", h, url)
            self.assertNotIn("Cookie", h, url)

    def test_referer_survives_other_origin(self):
        """Referer kimlik degil: CDN'ler hotlink denetimi icin bakar."""
        opt = _creds("http://a.test/basla")
        opt.headers["Referer"] = "http://a.test/sayfa"
        for url in ("http://a.test/parca", "http://b.test/parca"):
            self.assertEqual(opt.headers_for(url).get("Referer"),
                             "http://a.test/sayfa", url)
        other = opt.headers_for("http://b.test/parca")
        self.assertNotIn("Authorization", other)
        self.assertNotIn("Cookie", other)

    def test_proxy_authorization_survives(self):
        """Proxy-Authorization gecidin kimligidir: hedefe gore atilmaz."""
        opt = _creds("http://a.test/basla")
        opt.headers["Proxy-Authorization"] = "Basic cHJveHk="
        h = opt.headers_for("http://b.test/parca")
        self.assertEqual(h["Proxy-Authorization"], "Basic cHJveHk=")

    def test_no_scope_keeps_legacy_behaviour(self):
        """Kapsam bos (tek kaynak, ad-hoc): istek URL'inin kokeni gecerli."""
        opt = httpcli.DlOptions(user="u", password="p")
        self.assertIn("Authorization", opt.headers_for("http://b.test/x"))


class OpenerWiringTests(unittest.TestCase):
    def _handlers(self, opt, direct=False):
        return httpcli._make_opener(opt, direct=direct).handlers

    def test_scoped_redirect_replaces_stdlib_handler(self):
        for opt, direct in ((httpcli.DlOptions(), False),
                            (httpcli.DlOptions(), True),
                            (httpcli.DlOptions(proxy="socks5://127.0.0.1:9"),
                             False)):
            types = [type(h) for h in self._handlers(opt, direct)]
            self.assertIn(httpcli.ScopedRedirectHandler, types)
            self.assertNotIn(urllib.request.HTTPRedirectHandler, types,
                             "stdlib yonlendirme handler'i da kurulmamali")

    def test_no_password_manager_in_opener(self):
        """urllib'in parola deposu yok: kimlik yalniz headers_for'dan gelir.

        Global bir HTTPBasicAuthHandler bilinmeyen bir kokenin 401'ine
        kimlikle karsilik verebilirdi."""
        handlers = self._handlers(httpcli.DlOptions(user="u", password="p"))
        for h in handlers:
            self.assertNotIsInstance(h, urllib.request.HTTPBasicAuthHandler)
            self.assertNotIsInstance(h, urllib.request.HTTPDigestAuthHandler)


class RedirectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = RecordingServer(files={"/parca": DATA})
        cls.b = RecordingServer(files={"/parca": DATA})
        cls.addClassCleanup(cls.a.close)
        cls.addClassCleanup(cls.b.close)
        cls.a.server.redirects.update({
            "/basla": f"{cls.b.base}/parca",      # yabanci koken
            "/ic": f"{cls.a.base}/parca",         # ayni koken
            "/hop1": f"{cls.b.base}/hop2",        # A -> B -> A zinciri
        })
        cls.b.server.redirects.update({"/hop2": f"{cls.a.base}/parca"})

    def setUp(self):
        # kayitlar testler arasinda birikmesin (her test kendi istegine bakar)
        for srv in (self.a, self.b):
            with srv.server.lock:
                srv.server.seen.clear()

    def test_cross_origin_redirect_strips_credentials(self):
        referer = f"{self.a.base}/sayfa"
        opt = _creds(f"{self.a.base}/basla", headers={"Referer": referer})
        with httpcli.open_range(f"{self.a.base}/basla", opt, 0, 1023,
                                '"v1"') as res:
            self.assertEqual(res.read(1024), DATA[:1024])
        self.assertIn("authorization", self.a.headers("/basla")[0],
                      "kimlik sahibi koken kimligi almali")
        seen = self.b.headers("/parca")
        self.assertTrue(seen, "yabanci koken istek almali")
        for h in seen:
            # kimlik basliklari GITMEZ
            self.assertNotIn("authorization", h)
            self.assertNotIn("cookie", h)
            self.assertNotIn("x-ferdim-ozel", h)
            # kimlik olmayanlar KALIR: indirme ve dogrulama surer
            self.assertEqual(h.get("accept-encoding"), "identity")
            self.assertEqual(h.get("range"), "bytes=0-1023")
            self.assertEqual(h.get("if-range"), '"v1"')
            self.assertEqual(h.get("referer"), referer)

    def test_resume_through_cross_origin_redirect_sends_validator(self):
        """Devam eden indirme: dogrulayici yabanci koken hedefine GIDER.

        Surum sayfasi her istegi CDN'e yonlendirir ve dogrulayici (ETag) o
        yanittan ogrenilir. If-Range burada atilsaydi dosya degistiginde
        karisik devam riski dogardi (kimlik degil, dogrulama bilgisi)."""
        opt = _creds(f"{self.a.base}/basla")
        with httpcli.open_range(f"{self.a.base}/basla", opt, 1024, 2047,
                                '"v1"') as res:
            self.assertEqual(res.geturl(), f"{self.b.base}/parca")
            self.assertEqual(res.status, 206)
            self.assertEqual(res.read(1024), DATA[1024:2048])
        seen = self.b.headers("/parca")
        self.assertTrue(seen)
        for h in seen:
            self.assertEqual(h.get("range"), "bytes=1024-2047")
            self.assertEqual(h.get("if-range"), '"v1"')
            self.assertNotIn("authorization", h)
            self.assertNotIn("cookie", h)

    def test_same_origin_redirect_keeps_credentials(self):
        opt = _creds(f"{self.a.base}/ic")
        with httpcli.open_range(f"{self.a.base}/ic", opt, 0, 1023) as res:
            self.assertEqual(res.read(1024), DATA[:1024])
        seen = self.a.headers("/parca")
        self.assertTrue(seen)
        for h in seen:
            self.assertIn("authorization", h)
            self.assertEqual(h.get("cookie"), "oturum=abc")
            self.assertEqual(h.get("x-ferdim-ozel"), "gizli")

    def test_chain_back_to_origin_stays_stripped(self):
        """A -> B -> A: kimlik B'de atildi, A'ya donuste geri gelmez."""
        opt = _creds(f"{self.a.base}/hop1")
        with httpcli.open_range(f"{self.a.base}/hop1", opt, 0, 1023) as res:
            self.assertEqual(res.read(1024), DATA[:1024])
        self.assertIn("authorization", self.a.headers("/hop1")[0])
        for h in self.a.headers("/parca"):
            self.assertNotIn("authorization", h)
            self.assertNotIn("cookie", h)

    def test_redirect_decision_strips_on_https_downgrade(self):
        """https->http ayni ana makinede de kimlik atilir (TLS yok)."""
        handler = httpcli.ScopedRedirectHandler()
        req = urllib.request.Request("https://ornek.test/gizli",
                                     headers={"Authorization": "Basic x",
                                              "Cookie": "a=1",
                                              "Range": "bytes=0-1",
                                              "If-Range": '"v1"',
                                              "Referer":
                                                  "https://ornek.test/s"})
        new = handler.redirect_request(req, None, 302, "Found", {},
                                       "http://ornek.test/gizli")
        got = {k.lower(): v for k, v in new.headers.items()}
        self.assertNotIn("authorization", got)
        self.assertNotIn("cookie", got)
        self.assertEqual(got.get("range"), "bytes=0-1")
        self.assertEqual(got.get("if-range"), '"v1"',
                         "dogrulayici dususte de kalir")
        self.assertEqual(got.get("referer"), "https://ornek.test/s")
        self.assertEqual(new._ferdim_origin,
                         httpcli.origin_of("https://ornek.test/gizli"))
        # ayni sema/ana makine/port: kimlik korunur
        same = handler.redirect_request(req, None, 302, "Found", {},
                                        "https://ornek.test/obur")
        self.assertIn("Authorization", same.headers)

    def test_h3_redirect_falls_back_to_scoped_tcp(self):
        """h3 yonlendirmeyi izlemez: TCP'ye duser ve koken kurali gecerli.

        (aioquic gerekmez; h3 secimi ve istegi taklit edilir.)"""
        opt = _creds(f"{self.a.base}/basla")
        with mock.patch.object(h3, "use_h3", return_value=True), \
                mock.patch.object(h3, "request",
                                  side_effect=h3.H3Error("yonlendirme")):
            with httpcli.open_range(f"{self.a.base}/basla", opt, 0,
                                    1023) as res:
                self.assertEqual(res.read(1024), DATA[:1024])
        for h in self.b.headers("/parca"):
            self.assertNotIn("authorization", h)
            self.assertNotIn("cookie", h)


class MirrorScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = AppEnv(root=tmp_root(cls, "ferdim-redirect-"))
        cls.cfg = cls.env.load_config()
        cls.cfg["max_concurrent"] = 10
        cls.primary = RecordingServer(files={"/dosya.bin": DATA,
                                             "/ikiz.bin": DATA})
        cls.addClassCleanup(cls.primary.close)

    def _job(self, **kw):
        spec = JobSpec(**kw)
        return DownloadJob(self.env, self.cfg, spec)

    def _run(self, job):
        with warnings.catch_warnings():
            # yan dosya aramalarinin 404 HTTPError'i kapanmadigi icin
            # (motorun bilinen davranisi) ResourceWarning susar
            warnings.simplefilter("ignore", ResourceWarning)
            job.start()
            self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR)),
                            f"suren durum: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(
            hashlib.sha256(Path(job.final_path).read_bytes()).hexdigest(),
            DATA_SHA)

    def test_cross_origin_mirror_gets_no_credentials(self):
        mirror = RecordingServer(files={"/dosya.bin": DATA})
        self.addCleanup(mirror.close)
        job = self._job(url=f"{self.primary.base}/dosya.bin",
                        mirrors=[f"{mirror.base}/dosya.bin"], segments=8,
                        user="kullanici", password="parola",
                        cookies="oturum=abc",
                        headers={"X-Ferdim-Ozel": "gizli"})
        self._run(job)
        mine = self.primary.headers("/dosya.bin")
        self.assertTrue(any("authorization" in h for h in mine))
        theirs = mirror.headers("/dosya.bin")
        self.assertTrue(theirs, "ayna en az yoklanmali")
        for h in theirs:
            self.assertNotIn("authorization", h)
            self.assertNotIn("cookie", h)
            self.assertNotIn("x-ferdim-ozel", h)

    def test_same_origin_mirror_keeps_credentials(self):
        job = self._job(url=f"{self.primary.base}/dosya.bin",
                        mirrors=[f"{self.primary.base}/ikiz.bin"],
                        segments=8, user="kullanici", password="parola",
                        cookies="oturum=abc",
                        headers={"X-Ferdim-Ozel": "gizli"})
        self._run(job)
        seen = self.primary.headers("/ikiz.bin")
        self.assertTrue(seen, "ayni kokenli ayna istek almali")
        for h in seen:
            self.assertIn("authorization", h)
            self.assertEqual(h.get("cookie"), "oturum=abc")

    def test_job_completes_after_cross_origin_redirect(self):
        """Yabanci kokenli yonlendirme indirmeyi bozmaz, kimlik sizmaz."""
        far = RecordingServer(files={"/parca.bin": DATA})
        self.addCleanup(far.close)
        near = RecordingServer(redirects={
            "/basla": f"{far.base}/parca.bin"})
        self.addCleanup(near.close)
        job = self._job(url=f"{near.base}/basla", segments=4,
                        user="kullanici", password="parola",
                        cookies="oturum=abc")
        self._run(job)
        self.assertTrue(any("authorization" in h
                            for h in near.headers("/basla")))
        for h in far.headers():
            self.assertNotIn("authorization", h)
            self.assertNotIn("cookie", h)


if __name__ == "__main__":
    unittest.main()
