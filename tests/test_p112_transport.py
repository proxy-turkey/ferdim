"""ROADMAP 1.12 tasima: paylasilan TLS ayarlari, IDNA anahtarlari, probe dususu.

Kapsam:
  * TLS: ozel CA paketi, mTLS istemci sertifikasi, asgari TLS surumu.
    HTTP (urllib/ssl), h3 (aioquic QuicConfiguration) ve FTPS ayni ayari
    paylasir. Sertifikalar openssl CLI ile uretilir; openssl yoksa atlanir.
  * IDNA: profil anahtari, kimlik/cerez kapsami ve h3 origin'i ayni
    sunucunun unicode/punycode yazimlarinda birlesir.
  * Probe: bytes=0-0 reddedilirse duz GET ile bir kez denenir ve kaynak
    Range'siz tek akis sayilir.

Butun ag testleri yerel sunucular kullanir (internet yok, uyku yok).
"""
from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import api, paths
from ferdim.engine import ftp as ftp_mod
from ferdim.engine import h3, httpcli
from ferdim.paths import AppEnv
from ferdim.profiles import HostProfiles, host_key, normalise_host
from tests.helpers import tmp_root

OPENSSL = shutil.which("openssl")


# --------------------------------------------------------------- TLS dosyalari
def _openssl(*args: str) -> None:
    subprocess.run([OPENSSL, *args], check=True, capture_output=True)


class _Certs:
    """Tek bir test CA'si ve ondan turetilen sunucu/istemci sertifikalari."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.ca = root / "ca.pem"
        self.ca_key = root / "ca.key"
        self.server = root / "server.pem"
        self.server_key = root / "server.key"
        self.client = root / "client.pem"
        self.client_key = root / "client.key"
        self.client_combined = root / "client-both.pem"
        self._build()

    def _build(self) -> None:
        _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(self.ca_key), "-out", str(self.ca),
                 "-days", "1", "-subj", "/CN=FerDim Test CA",
                 "-addext", "basicConstraints=critical,CA:TRUE",
                 "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                 # siki X.509 dogrulamasi (Python 3.13+ VERIFY_X509_STRICT)
                 # anahtar kimliklerini ister; her openssl surumu eklemez
                 "-addext", "subjectKeyIdentifier=hash")
        self._leaf("server", "serverAuth", "IP:127.0.0.1,DNS:localhost",
                   self.server, self.server_key)
        self._leaf("client", "clientAuth", None,
                   self.client, self.client_key)
        # anahtar sertifika dosyasindaysa tls_client_key bos kalabilir
        self.client_combined.write_bytes(
            self.client.read_bytes() + self.client_key.read_bytes())

    def _leaf(self, name: str, usage: str, san: str | None,
              cert: Path, key: Path) -> None:
        csr = self.root / f"{name}.csr"
        ext = self.root / f"{name}.ext"
        lines = ["basicConstraints=CA:FALSE",
                 "keyUsage=digitalSignature,keyEncipherment",
                 f"extendedKeyUsage={usage}",
                 "subjectKeyIdentifier=hash",
                 "authorityKeyIdentifier=keyid,issuer"]
        if san:
            lines.append(f"subjectAltName={san}")
        ext.write_text("\n".join(lines) + "\n", "utf-8")
        _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                 "-out", str(csr), "-subj", "/CN=localhost")
        _openssl("x509", "-req", "-in", str(csr), "-CA", str(self.ca),
                 "-CAkey", str(self.ca_key), "-CAcreateserial",
                 "-out", str(cert), "-days", "1", "-sha256",
                 "-extfile", str(ext))


# ------------------------------------------------------------ yerel sunucular
BODY = b"0123456789" * 4096


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        path = self.path.split("?", 1)[0]
        srv.seen.append((path, self.headers.get("Range")))
        mode = srv.modes.get(path, "ok")
        has_range = bool(self.headers.get("Range"))
        if mode in ("range-405", "range-416") and has_range:
            self._reply(405 if mode == "range-405" else 416, b"range yok")
            return
        if mode == "always-405":
            self._reply(405, b"yok")
            return
        if mode == "missing":
            self._reply(404, b"yok")
            return
        self._reply(200, srv.body,
                    extra=[("Accept-Ranges", "bytes"),
                           ("ETag", '"p112"')])

    def _reply(self, code: int, body: bytes, extra=()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        for key, val in extra:
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(body)


class _HttpServer:
    def __init__(self, modes: dict | None = None) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.modes = modes or {}
        self.httpd.body = BODY
        self.httpd.seen = []
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def seen(self) -> list:
        return self.httpd.seen

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


class _TlsHandler(_Handler):
    pass


class _HttpsServer:
    def __init__(self, cert, key, *, client_ca=None,
                 maximum_version=None) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _TlsHandler)
        self.httpd.modes = {}
        self.httpd.body = BODY
        self.httpd.seen = []
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        if client_ca is not None:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cafile=str(client_ca))
        if maximum_version is not None:
            ctx.maximum_version = maximum_version
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket,
                                            server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


def _probe(url: str, **kw) -> httpcli.ProbeResult:
    return httpcli.probe(url, httpcli.DlOptions(timeout=5, **kw))


# ---------------------------------------------------------------------- TLS
@unittest.skipUnless(OPENSSL, "openssl komutu yok")
class TlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tmp_root(cls, "ferdim-p112-tls-")
        cls.certs = _Certs(cls.root)
        # ayar basina baglam onbellegi testler arasinda tasinmasin
        httpcli._context_for.cache_clear()

    @classmethod
    def tearDownClass(cls):
        httpcli._context_for.cache_clear()

    def setUp(self):
        httpcli._context_for.cache_clear()

    def _server(self, **kw) -> _HttpsServer:
        srv = _HttpsServer(self.certs.server, self.certs.server_key, **kw)
        self.addCleanup(srv.close)
        return srv

    def test_custom_ca_added_to_default_trust(self):
        srv = self._server()
        url = srv.url + "/dosya.bin"
        with self.assertRaises((urllib.error.URLError, ssl.SSLError)):
            _probe(url)
        p = _probe(url, tls=httpcli.TlsConfig(ca_file=str(self.certs.ca)))
        self.assertEqual(p.status, 200)
        self.assertEqual(p.total, len(BODY))

    def test_client_certificate_required(self):
        srv = self._server(client_ca=self.certs.ca)
        url = srv.url + "/dosya.bin"
        with self.assertRaises((urllib.error.URLError, ssl.SSLError)):
            _probe(url, tls=httpcli.TlsConfig(ca_file=str(self.certs.ca)))
        p = _probe(url, tls=httpcli.TlsConfig(
            ca_file=str(self.certs.ca),
            client_cert=str(self.certs.client),
            client_key=str(self.certs.client_key)))
        self.assertEqual(p.status, 200)
        # anahtar sertifika dosyasindaysa ayri anahtar gerekmez
        p = _probe(url, tls=httpcli.TlsConfig(
            ca_file=str(self.certs.ca),
            client_cert=str(self.certs.client_combined)))
        self.assertEqual(p.status, 200)

    def test_minimum_tls_version_enforced(self):
        srv = self._server(maximum_version=ssl.TLSVersion.TLSv1_2)
        url = srv.url + "/dosya.bin"
        with self.assertRaises((urllib.error.URLError, ssl.SSLError)):
            _probe(url, tls=httpcli.TlsConfig(ca_file=str(self.certs.ca),
                                              min_version="1.3"))
        p = _probe(url, tls=httpcli.TlsConfig(ca_file=str(self.certs.ca),
                                              min_version="1.2"))
        self.assertEqual(p.status, 200)

    def test_context_is_cached_per_settings(self):
        tls = httpcli.TlsConfig(ca_file=str(self.certs.ca),
                                min_version="1.3")
        a = httpcli.ssl_context(tls)
        b = httpcli.ssl_context(httpcli.DlOptions(
            tls=httpcli.TlsConfig(ca_file=str(self.certs.ca),
                                  min_version="1.3")))
        self.assertIs(a, b)                     # ayni ayar = ayni nesne
        self.assertEqual(a.minimum_version, ssl.TLSVersion.TLSv1_3)
        der = ssl.PEM_cert_to_DER_cert(self.certs.ca.read_text("utf-8"))
        self.assertIn(der, a.get_ca_certs(binary_form=True))

    def test_bad_tls_path_has_config_code(self):
        opt = httpcli.DlOptions(tls=httpcli.TlsConfig(
            ca_file=str(self.root / "yok.pem")))
        with self.assertRaises(httpcli.TlsConfigError) as cm:
            httpcli.ssl_context(opt)
        self.assertEqual(cm.exception.code, "tls_config")

    def test_ftp_uses_shared_context_and_error_code(self):
        tls = httpcli.TlsConfig(ca_file=str(self.certs.ca), min_version="1.3")
        self.assertIs(ftp_mod._ssl_context(tls, False),
                      httpcli.ssl_context(tls))
        err = ftp_mod._wrap(httpcli.TlsConfigError("okunamadi"))
        self.assertEqual(err.code, "tls_config")

    def test_h3_quic_config_applies_ca_and_client_cert(self):
        class Conf:
            def __init__(self, **kw):
                self.kw = kw
                self.cafile = None
                self.chain = None

            def load_verify_locations(self, cafile=None, capath=None,
                                      cadata=None):
                self.cafile = cafile

            def load_cert_chain(self, certfile, keyfile=None, password=None):
                self.chain = (certfile, keyfile)

        api_symbols = {"QuicConfiguration": Conf, "H3_ALPN": ["h3"]}
        tls = httpcli.TlsConfig(ca_file=str(self.certs.ca),
                                client_cert=str(self.certs.client),
                                client_key=str(self.certs.client_key))
        with mock.patch.object(h3, "_load", return_value=api_symbols):
            conf = h3._quic_config("example.test", False, tls)
        self.assertEqual(conf.cafile, str(self.certs.ca))
        self.assertEqual(conf.chain,
                         (str(self.certs.client), str(self.certs.client_key)))
        self.assertEqual(conf.kw["server_name"], "example.test")

    def test_context_cache_invalidates_when_file_changes(self):
        tls = httpcli.TlsConfig(ca_file=str(self.certs.ca))
        first_fp = tls.fingerprint()
        a = httpcli.ssl_context(tls)
        st = os.stat(str(self.certs.ca))
        os.utime(str(self.certs.ca),
                 ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        self.assertNotEqual(tls.fingerprint(), first_fp)
        b = httpcli.ssl_context(tls)
        self.assertIsNot(a, b, "degisen CA yeni baglam uretmeli")

    def test_h3_pool_key_includes_tls_fingerprint(self):
        opt = httpcli.DlOptions(
            tls=httpcli.TlsConfig(ca_file=str(self.certs.ca)))
        url = "https://example.test/x.bin"
        k1 = h3._key_of(url, opt, None)
        st = os.stat(str(self.certs.ca))
        os.utime(str(self.certs.ca),
                 ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000))
        k2 = h3._key_of(url, opt, None)
        self.assertEqual(len(k1), 5)
        self.assertEqual(k1[:4], k2[:4], "hedef/SNI/mod degismemeli")
        self.assertNotEqual(k1, k2, "TLS ayar izi havuz anahtarina girmeli")


class TlsConfigSanitizeTests(unittest.TestCase):
    def setUp(self):
        self.root = tmp_root(self, "ferdim-p112-tls-cfg-")
        self.file = self.root / "ca.pem"
        self.file.write_text("test", "utf-8")

    def test_defaults_are_empty(self):
        for key in ("tls_ca_file", "tls_client_cert", "tls_client_key",
                    "tls_min_version"):
            self.assertEqual(paths.DEFAULTS[key], "")

    def test_valid_paths_and_version_kept(self):
        cfg = dict(paths.DEFAULTS)
        real = os.path.realpath(str(self.file))
        cfg.update(tls_ca_file=str(self.file),
                   tls_client_cert=str(self.file),
                   tls_client_key=str(self.file),
                   tls_min_version="1.3")
        self.assertEqual(api._sanitize_config(cfg), "")
        self.assertEqual(cfg["tls_ca_file"], real)
        self.assertEqual(cfg["tls_min_version"], "1.3")

    def test_bad_values_dropped_with_warning(self):
        cfg = dict(paths.DEFAULTS)
        cfg.update(tls_ca_file="goreli/yol.pem",
                   tls_min_version="1.1")
        self.assertEqual(api._sanitize_config(cfg), "tls_config")
        self.assertEqual(cfg["tls_ca_file"], "")
        self.assertEqual(cfg["tls_min_version"], "")

    def test_key_without_cert_is_dropped(self):
        cfg = dict(paths.DEFAULTS)
        cfg.update(tls_client_key=str(self.file))
        self.assertEqual(api._sanitize_config(cfg), "tls_config")
        self.assertEqual(cfg["tls_client_key"], "")

    def test_config_set_reports_warning(self):
        env = AppEnv(root=tmp_root(self, "ferdim-p112-tls-api-"))
        cfg = env.load_config()
        ctx = {"qm": None, "env": env, "cfg": cfg,
               "set_cfg": env.save_config}
        r = api.handle(ctx, "config-set",
                       {"config": {"tls_ca_file": str(self.root / "yok")}})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r.get("warning"), "tls_config")
        self.assertEqual(cfg["tls_ca_file"], "")
        r = api.handle(ctx, "config-set",
                       {"config": {"tls_ca_file": str(self.file)}})
        self.assertNotIn("warning", r)
        self.assertEqual(cfg["tls_ca_file"],
                         os.path.realpath(str(self.file)))

    def test_tls_file_keys_are_socket_only(self):
        env = AppEnv(root=tmp_root(self, "ferdim-p112-tls-http-"))
        cfg = env.load_config()
        real = os.path.realpath(str(self.file))
        http_ctx = {"qm": None, "env": env, "cfg": cfg,
                    "set_cfg": env.save_config, "transport": "http",
                    "restrict_paths": True}
        for key in ("tls_ca_file", "tls_client_cert", "tls_client_key"):
            r = api.handle(http_ctx, "config-set",
                           {"config": {key: str(self.file)}})
            self.assertFalse(r.get("ok"), (key, r))
            self.assertEqual(r.get("code"), "setting_socket_only")
            self.assertEqual(cfg[key], "")
        # ayni degeri yeniden gondermek gecer (web tum config'i gonderir)
        cfg["tls_ca_file"] = real
        r = api.handle(http_ctx, "config-set",
                       {"config": {"tls_ca_file": real}})
        self.assertTrue(r.get("ok"), r)
        # soket/CLI yolu serbest
        sock_ctx = {k: v for k, v in http_ctx.items()
                    if k not in ("transport", "restrict_paths")}
        cfg["tls_ca_file"] = ""
        r = api.handle(sock_ctx, "config-set",
                       {"config": {"tls_ca_file": real}})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(cfg["tls_ca_file"], real)


class TlsFileValidationTests(unittest.TestCase):
    def setUp(self):
        self.root = tmp_root(self, "ferdim-p112-tls-file-")

    def test_regular_owned_file_is_accepted(self):
        p = self.root / "ca.pem"
        p.write_text("x", "utf-8")
        os.chmod(p, 0o600)
        self.assertEqual(httpcli.tls_file_path(str(p)),
                         os.path.realpath(str(p)))

    def test_relative_and_missing_rejected(self):
        self.assertEqual(httpcli.tls_file_path("ca.pem"), "")
        self.assertEqual(httpcli.tls_file_path(str(self.root / "yok")), "")

    def test_fifo_rejected(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo yok")
        p = self.root / "fifo"
        os.mkfifo(p)
        self.assertEqual(httpcli.tls_file_path(str(p)), "")

    def test_device_rejected(self):
        if not os.path.exists("/dev/zero"):
            self.skipTest("/dev/zero yok")
        self.assertEqual(httpcli.tls_file_path("/dev/zero"), "")

    def test_world_or_group_writable_rejected(self):
        p = self.root / "w.pem"
        p.write_text("x", "utf-8")
        os.chmod(p, 0o666)
        self.assertEqual(httpcli.tls_file_path(str(p)), "")
        os.chmod(p, 0o664)
        self.assertEqual(httpcli.tls_file_path(str(p)), "")

    def test_oversized_rejected(self):
        p = self.root / "big.pem"
        with open(p, "wb") as f:
            f.truncate(httpcli.MAX_TLS_FILE + 1)
        os.chmod(p, 0o600)
        self.assertEqual(httpcli.tls_file_path(str(p)), "")

    def test_symlink_resolved(self):
        target = self.root / "real.pem"
        target.write_text("x", "utf-8")
        os.chmod(target, 0o600)
        link = self.root / "link.pem"
        os.symlink(target, link)
        self.assertEqual(httpcli.tls_file_path(str(link)),
                         os.path.realpath(str(target)))

    def test_sanitize_uses_same_rules(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo yok")
        p = self.root / "fifo"
        os.mkfifo(p)
        cfg = dict(paths.DEFAULTS)
        cfg["tls_ca_file"] = str(p)
        self.assertEqual(api._sanitize_config(cfg), "tls_config")
        self.assertEqual(cfg["tls_ca_file"], "")


class RequestSchemeTests(unittest.TestCase):
    def test_request_of_rejects_non_http(self):
        opt = httpcli.DlOptions()
        for url in ("ftp://example.org/a", "file:///etc/passwd", "",
                    "mailto:a@b"):
            with self.assertRaises(ValueError):
                httpcli.request_of(url, opt)

    def test_request_of_accepts_http_and_https(self):
        opt = httpcli.DlOptions()
        for url in ("http://example.org/a", "https://example.org/a",
                    "HTTP://example.org/a"):
            self.assertIsNotNone(httpcli.request_of(url, opt))


# --------------------------------------------------------------------- IDNA
class IdnaTests(unittest.TestCase):
    def test_normalise_host(self):
        self.assertEqual(normalise_host("Bücher.Example"),
                         "xn--bcher-kva.example")
        self.assertEqual(normalise_host("xn--bcher-kva.example"),
                         "xn--bcher-kva.example")
        self.assertEqual(normalise_host("Example.COM."), "example.com")
        self.assertEqual(normalise_host("[::1]"), "::1")
        self.assertEqual(normalise_host("::1"), "::1")
        self.assertEqual(normalise_host(""), "")
        self.assertEqual(normalise_host("../etc"), "")

    def test_host_key_spellings_match(self):
        self.assertEqual(host_key("https://Bücher.example/x"),
                         "xn--bcher-kva.example")
        self.assertEqual(host_key("https://xn--bcher-kva.example./y"),
                         "xn--bcher-kva.example")
        self.assertEqual(host_key("Bücher.example:8080"),
                         "xn--bcher-kva.example:8080")

    def test_origin_matches_unicode_and_punycode(self):
        a = httpcli.origin_of("https://Bücher.example/x")
        b = httpcli.origin_of("https://xn--bcher-kva.example/x")
        self.assertEqual(a, b)
        self.assertTrue(
            httpcli.same_origin(a, "https://xn--bcher-kva.example/y"))

    def test_credentials_follow_idna_origin(self):
        opt = httpcli.DlOptions(
            user="u", password="p", cookies="oturum=1",
            origin=httpcli.origin_of("https://Bücher.example/a"))
        same = opt.headers_for("https://xn--bcher-kva.example/b")
        self.assertIn("Authorization", same)
        self.assertEqual(same.get("Cookie"), "oturum=1")
        other = opt.headers_for("https://baska.example/b")
        self.assertNotIn("Authorization", other)
        self.assertNotIn("Cookie", other)

    def test_profiles_store_single_key(self):
        env = AppEnv(root=tmp_root(self, "ferdim-p112-idna-"))
        store = HostProfiles(env)
        self.assertTrue(store.record("https://Bücher.example/x", links=5))
        e = store.get("https://xn--bcher-kva.example/y")
        self.assertIsNotNone(e)
        self.assertEqual(e["host"], "xn--bcher-kva.example")
        data = json.loads((env.state_dir / "hosts.json").read_text("utf-8"))
        self.assertEqual(list(data["hosts"]), ["xn--bcher-kva.example"])

    def test_legacy_unicode_key_normalised_on_load(self):
        env = AppEnv(root=tmp_root(self, "ferdim-p112-idna-load-"))
        legacy = {"version": 1, "hosts": {"Bücher.example": {
            "links": 4, "last_seen": time.time()}}}
        (env.state_dir / "hosts.json").write_text(json.dumps(legacy), "utf-8")
        store = HostProfiles(env)
        self.assertIsNotNone(store.get("xn--bcher-kva.example"))
        # unicode yazim da ayni kayda normalise olur
        self.assertEqual(store.get("Bücher.example")["host"],
                         "xn--bcher-kva.example")

    def test_h3_origin_normalised(self):
        self.assertEqual(h3.origin("https://Bücher.example/x"),
                         h3.origin("https://xn--bcher-kva.example/x"))


# -------------------------------------------------------------------- probe
class ProbeFallbackTests(unittest.TestCase):
    def _server(self, modes) -> _HttpServer:
        srv = _HttpServer(modes)
        self.addCleanup(srv.close)
        return srv

    def test_range_rejection_falls_back_to_plain_get(self):
        srv = self._server({"/dosya.bin": "range-405"})
        p = _probe(srv.url + "/dosya.bin")
        self.assertEqual(p.status, 200)
        self.assertEqual(p.total, len(BODY))
        self.assertFalse(p.accept_ranges, "tek akis sayilmali")
        self.assertEqual(srv.seen[0][1], "bytes=0-0")
        self.assertIsNone(srv.seen[-1][1], "duz GET Range gondermemeli")
        self.assertEqual(len(srv.seen), 2)

    def test_416_also_falls_back(self):
        srv = self._server({"/bos.bin": "range-416"})
        p = _probe(srv.url + "/bos.bin")
        self.assertFalse(p.accept_ranges)
        self.assertEqual(p.total, len(BODY))

    def test_plain_404_is_not_retried(self):
        srv = self._server({"/yok": "missing"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            _probe(srv.url + "/yok")
        self.assertEqual(cm.exception.code, 404)
        self.assertEqual(len(srv.seen), 1, "404 Range hatasi degildir")

    def test_failed_plain_get_keeps_error(self):
        srv = self._server({"/hep": "always-405"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            _probe(srv.url + "/hep")
        self.assertEqual(cm.exception.code, 405)
        self.assertEqual(len(srv.seen), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
