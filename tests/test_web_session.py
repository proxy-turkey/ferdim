"""Web oturumu: tek kullanimlik bilet -> token, token donusumu, soket siniri.

Sayfa artik token tasimaz. Tarayici `web-session` komutunun verdigi
`#s=<nonce>` adresiyle acilir; bilet POST /api/session'da BIR KEZ token'a
cevrilir. Bilet 120 sn yasar, en fazla 16 bilet bekler. `token-rotate`
kosan sunucuda aninda etkilidir (eski token 401 alir).
"""
from __future__ import annotations

import json
import os
import socket
import stat
import sys
import threading
import unittest
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import api
from ferdim.engine.queue import QueueManager
from ferdim.paths import AppEnv
from ferdim.server.sock import MAX_RPC_LINE, SockServer
from ferdim.server.web import make_server
from tests.helpers import tmp_root
from tests.test_web_security import _raw, _raw_full, _request

TOKEN = "testtoken"


def _socket_rpc(path: str, obj: dict) -> dict:
    """Unix soketine tek istek atip tek satir yanit okur."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(path)
        s.sendall(json.dumps(obj).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            part = s.recv(65536)
            if not part:
                break
            buf += part
    return json.loads(buf.split(b"\n", 1)[0])


class WebSessionTests(unittest.TestCase):
    """Bilet omru/tek kullanim/sinir ve canli sunucuda token donusumu."""

    @classmethod
    def setUpClass(cls):
        cls.env = AppEnv(root=tmp_root(cls, "ferdim-websession-"))
        cls.cfg = cls.env.load_config()
        cls.qm = QueueManager(cls.env, cls.cfg)
        cls.addClassCleanup(cls.qm.shutdown)
        cls.ctx = {"qm": cls.qm, "env": cls.env, "cfg": cls.cfg,
                   "set_cfg": cls.env.save_config}
        cls.env.token_path.write_text(TOKEN + "\n")
        cls.httpd = make_server(cls.env, cls.ctx, 0)
        cls.port = cls.httpd.server_address[1]
        cls.ctx["web_port"] = cls.port      # daemon bunu baglaninca yazar
        cls.addClassCleanup(cls.httpd.server_close)
        cls.addClassCleanup(cls.httpd.shutdown)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.host = f"127.0.0.1:{cls.port}"

    def setUp(self):
        self.access = api.web_access(self.ctx)
        self._clock = self.access.clock
        with self.access.lock:
            self.access.nonces.clear()

    def tearDown(self):
        self.access.clock = self._clock
        with self.access.lock:
            self.access.nonces.clear()

    # ------------------------------------------------------------- yardimci
    def _mint(self) -> str:
        r = api.handle(self.ctx, "web-session", {})
        self.assertTrue(r.get("ok"), r)
        url = urlparse(r["url"])
        self.assertEqual(url.port, self.port)
        self.assertEqual(url.path, "/")
        return url.fragment.split("=", 1)[1]

    def _exchange(self, nonce, host=None, origin=None, token=None):
        return _raw_full(self.port, _request(
            "/api/session", method="POST", host=host or self.host,
            origin=origin, token=token,
            body=json.dumps({"nonce": nonce}).encode()))

    def _code(self, body: bytes) -> str:
        try:
            return json.loads(body).get("code", "")
        except Exception:
            return ""

    # ---------------------------------------------------------------- testler
    def test_01_one_time_nonce_becomes_the_token(self):
        nonce = self._mint()
        status, _headers, body = self._exchange(nonce)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["token"], TOKEN)
        # ikinci kez: tuketildi
        status, _headers, body = self._exchange(nonce)
        self.assertEqual(status, 403)
        self.assertEqual(self._code(body), "session_invalid")
        # alinan token API'yi acar
        status, body = _raw(self.port, _request(
            "/api/stats", host=self.host, token=TOKEN))
        self.assertEqual(status, 200)
        self.assertIn(b'"jobs"', body)

    def test_02_nonce_expires_without_sleeping(self):
        now = [1000.0]
        self.access.clock = lambda: now[0]
        nonce = self._mint()
        now[0] += api.WEB_SESSION_TTL - 1
        self.assertEqual(self._exchange(nonce)[0], 200)
        # ayni bilet artik tuketilmis; yeni bilet omrunu doldurunca olur
        fresh = self._mint()
        now[0] += api.WEB_SESSION_TTL + 1
        status, _headers, body = self._exchange(fresh)
        self.assertEqual(status, 403)
        self.assertEqual(self._code(body), "session_invalid")

    def test_03_cap_keeps_newest_sixteen(self):
        nonces = [self._mint() for _ in range(api.WEB_SESSION_MAX + 1)]
        # en eski bilet dustu
        status, _headers, body = self._exchange(nonces[0])
        self.assertEqual(status, 403)
        self.assertEqual(self._code(body), "session_invalid")
        # en yeni ve ondan onceki hala gecerli
        self.assertEqual(self._exchange(nonces[-1])[0], 200)
        self.assertEqual(self._exchange(nonces[1])[0], 200)
        with self.access.lock:
            self.assertEqual(len(self.access.nonces),
                             api.WEB_SESSION_MAX - 2)

    def test_04_wrong_nonce_is_403(self):
        self._mint()
        for bad in ({"nonce": "yanlis-bilet"}, {"nonce": 42}, {},
                    {"nonce": "x" * 200}):
            with self.subTest(nonce=bad):
                body = json.dumps(bad).encode()
                status, _headers, raw = _raw_full(self.port, _request(
                    "/api/session", method="POST", host=self.host, body=body))
                self.assertEqual(status, 403)
                self.assertEqual(self._code(raw), "session_invalid")
        # bozuk JSON: 400
        status, _headers, _body = _raw_full(self.port, _request(
            "/api/session", method="POST", host=self.host, body=b"{bozuk"))
        self.assertEqual(status, 400)

    def test_05_session_route_needs_host_and_origin(self):
        nonce = self._mint()
        status, body = _raw(self.port, _request(
            "/api/session", method="POST", host="evil.example:1234",
            body=json.dumps({"nonce": nonce}).encode()))
        self.assertEqual(status, 421)
        self.assertEqual(body, b"")
        status, body = _raw(self.port, _request(
            "/api/session", method="POST", host=self.host,
            origin="https://evil.example",
            body=json.dumps({"nonce": nonce}).encode()))
        self.assertEqual(status, 403)
        self.assertEqual(body, b"")
        # dogru Host + yerel Origin: bilet hala gecerli (yukarida tuketilmedi)
        status, _headers, body = self._exchange(
            nonce, origin=f"http://{self.host}")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["token"], TOKEN)

    def test_06_i18n_is_public_everything_else_is_not(self):
        status, body = _raw(self.port, _request(
            "/api/i18n?lang=tr", host=self.host))
        self.assertEqual(status, 200)
        r = json.loads(body)
        self.assertTrue(r["ok"])
        self.assertEqual(r["lang"], "tr")
        self.assertTrue(r["strings"])
        # sorgu yoksa da okunur (dil config'ten)
        self.assertEqual(_raw(self.port, _request(
            "/api/i18n", host=self.host))[0], 200)
        # diger /api ve /metrics token ister
        for path in ("/api/stats", "/api/config", "/api/version", "/metrics"):
            with self.subTest(path=path):
                status, _headers, body = _raw_full(
                    self.port, _request(path, host=self.host))
                self.assertEqual(status, 401)
                self.assertNotIn(b"jobs", body)
                self.assertNotIn(TOKEN.encode(), body)

    def test_07_token_rotate_invalidates_old_token(self):
        self.assertEqual(_raw(self.port, _request(
            "/api/stats", host=self.host, token=TOKEN))[0], 200)
        r = api.handle(self.ctx, "token-rotate", {})
        self.assertTrue(r.get("ok"), r)
        new_token = self.env.token_path.read_text("utf-8").strip()
        self.assertNotEqual(new_token, TOKEN)
        self.assertEqual(self.access.get_token(), new_token)
        # dosya 0600 ve baska kimse okuyamaz
        self.assertEqual(stat.S_IMODE(self.env.token_path.stat().st_mode), 0o600)
        # eski token kosan sunucuda aninda gecersiz
        status, _headers, body = _raw_full(self.port, _request(
            "/api/stats", host=self.host, token=TOKEN))
        self.assertEqual(status, 401)
        self.assertNotIn(b"jobs", body)
        # yeni token gecerli
        status, body = _raw(self.port, _request(
            "/api/stats", host=self.host, token=new_token))
        self.assertEqual(status, 200)
        self.assertIn(b'"jobs"', body)
        # sonraki testler eski token'la devam edebilsin
        api.write_token(self.env.token_path, TOKEN)
        self.access.set_token(TOKEN)

    def test_08_bundle_is_not_reachable_over_http(self):
        # sokete ozel komutlar HTTP yol tablosunda yok; tabloya yanlislikla
        # eklenirse api katmani da reddeder (transport isareti)
        for path in ("/api/web-session", "/api/token-rotate"):
            with self.subTest(path=path):
                self.assertEqual(_raw(self.port, _request(
                    path, host=self.host, token=TOKEN))[0], 404)
                self.assertEqual(_raw(self.port, _request(
                    path, method="POST", host=self.host, token=TOKEN,
                    body=b"{}"))[0], 404)
        hctx = dict(self.ctx, restrict_paths=True, transport="http")
        for cmd in ("web-session", "token-rotate"):
            r = api.handle(hctx, cmd, {})
            self.assertFalse(r["ok"], r)

    def test_09_web_disabled_reports_code(self):
        r = api.handle(dict(self.ctx, web_port=0), "web-session", {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "web_disabled")
        self.assertTrue(r["error"])

    def test_10_token_rotation_drops_pending_nonces(self):
        """Bilet eski token icin verildi: donusumden sonra tuketilemez."""
        nonce = self._mint()
        self.assertTrue(api.handle(self.ctx, "token-rotate", {})["ok"])
        status, _headers, body = self._exchange(nonce)
        self.assertEqual(status, 403)
        self.assertEqual(self._code(body), "session_invalid")
        # sonraki testler icin token geri kurulur (set_token biletleri atar)
        api.write_token(self.env.token_path, TOKEN)
        self.access.set_token(TOKEN)


class SocketTransportTests(unittest.TestCase):
    """Soket: 1 MiB satir siniri, 0600 izin, komutlar gercekten calisir."""

    @classmethod
    def setUpClass(cls):
        cls.env = AppEnv(root=tmp_root(cls, "ferdim-socksec-"))
        cls.cfg = cls.env.load_config()
        cls.qm = QueueManager(cls.env, cls.cfg)
        cls.addClassCleanup(cls.qm.shutdown)
        cls.ctx = {"qm": cls.qm, "env": cls.env, "cfg": cls.cfg,
                   "set_cfg": cls.env.save_config,
                   "web_port": 8399}
        api.write_token(cls.env.token_path, TOKEN)
        cls.srv = SockServer(str(cls.env.socket_path), cls.ctx)
        cls.addClassCleanup(cls.srv.server_close)
        cls.addClassCleanup(cls.srv.shutdown)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.sock = str(cls.env.socket_path)

    def test_01_socket_is_private(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.sock).st_mode), 0o600)

    def test_02_web_session_over_socket(self):
        r = _socket_rpc(self.sock, {"cmd": "web-session", "req_id": 7})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r.get("req_id"), 7)
        url = urlparse(r["url"])
        self.assertEqual(url.port, 8399)
        self.assertTrue(url.fragment.startswith("s="))
        nonce = url.fragment.split("=", 1)[1]
        # bilet depoda: tek kullanimlik, dogru token'i verir
        self.assertEqual(api.web_access(self.ctx).consume(nonce), TOKEN)
        self.assertEqual(api.web_access(self.ctx).consume(nonce), "")

    def test_03_token_rotate_over_socket(self):
        before = self.env.token_path.read_text("utf-8").strip()
        r = _socket_rpc(self.sock, {"cmd": "token-rotate"})
        self.assertTrue(r.get("ok"), r)
        after = self.env.token_path.read_text("utf-8").strip()
        self.assertNotEqual(before, after)
        self.assertEqual(api.web_access(self.ctx).get_token(), after)
        self.assertEqual(stat.S_IMODE(self.env.token_path.stat().st_mode), 0o600)

    def test_04_oversized_line_is_rejected(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(20)
            s.connect(self.sock)
            # sonlandiricisiz 1 MiB ustu: okuma sinirda doner, baglanti kapanir
            s.sendall(b"x" * (MAX_RPC_LINE + 64))
            buf = b""
            while b"\n" not in buf:
                part = s.recv(65536)
                if not part:
                    break
                buf += part
            line = buf.split(b"\n", 1)[0]
            self.assertEqual(json.loads(line), {"ok": False, "code": "too_large"})
            # sunucu baglantiyi kapatti
            self.assertEqual(s.recv(65536), b"")
        # soket hala saglikli: sonraki istek yanit alir
        self.assertTrue(_socket_rpc(self.sock, {"cmd": "ping"})["ok"])


class TokenFileTests(unittest.TestCase):
    """Token dosyasi 0600; 1.7.1 yukseltmesinde bir kez yenilenir.

    Eski surumler token'i index.html'e gomuyordu: yukseltmede token
    yenilenir (isaret: state/token.v2) ve sizmis olabilecek deger gecersiz
    kalir. Sonraki acilislar token'i korur."""

    def test_01_legacy_token_is_rotated_once(self):
        from ferdim import daemon
        env = AppEnv(root=tmp_root(self, "ferdim-token-"))
        env.token_path.write_text("eski\n")
        os.chmod(env.token_path, 0o644)
        token = daemon.ensure_token(env)
        self.assertNotEqual(token, "eski", "eski token yenilenmeli")
        self.assertEqual(len(token), 32)
        self.assertEqual(stat.S_IMODE(env.token_path.stat().st_mode), 0o600)
        marker = env.state_dir / daemon.TOKEN_MARKER
        self.assertTrue(marker.exists(), "isaret dosyasi yok")
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        # bir kez: sonraki acilis token'i (ve iznini) korur
        self.assertEqual(daemon.ensure_token(env), token)

    def test_02_missing_token_is_created_private(self):
        from ferdim import daemon
        env = AppEnv(root=tmp_root(self, "ferdim-token2-"))
        token = daemon.ensure_token(env)
        self.assertEqual(len(token), 32)
        self.assertEqual(stat.S_IMODE(env.token_path.stat().st_mode), 0o600)
        self.assertTrue((env.state_dir / daemon.TOKEN_MARKER).exists())
        # ayni dosya: deger korunur
        self.assertEqual(daemon.ensure_token(env), token)

    def test_03_marker_keeps_token_and_tightens_perms(self):
        from ferdim import daemon
        env = AppEnv(root=tmp_root(self, "ferdim-token4-"))
        env.token_path.write_text("sabit\n")
        os.chmod(env.token_path, 0o644)
        daemon._write_marker(env.state_dir / daemon.TOKEN_MARKER)
        self.assertEqual(daemon.ensure_token(env), "sabit")
        self.assertEqual(stat.S_IMODE(env.token_path.stat().st_mode), 0o600)

    def test_03_rotation_is_atomic_and_private(self):
        env = AppEnv(root=tmp_root(self, "ferdim-token3-"))
        api.write_token(env.token_path, "bir")
        api.write_token(env.token_path, "iki")
        self.assertEqual(env.token_path.read_text("utf-8"), "iki")
        self.assertEqual(stat.S_IMODE(env.token_path.stat().st_mode), 0o600)
        # ayni dizinde gecici dosya kalmaz
        leftovers = [p.name for p in env.state_dir.iterdir()
                     if p.name.startswith(".token-")]
        self.assertEqual(leftovers, [])

    def test_04_log_file_is_private(self):
        from ferdim import daemon
        env = AppEnv(root=tmp_root(self, "ferdim-log-"))
        daemon.secure_log(env)
        self.assertEqual(stat.S_IMODE(env.log_path.stat().st_mode), 0o600)
        # var olan genis izinli log da daraltilir
        os.chmod(env.log_path, 0o644)
        daemon.secure_log(env)
        self.assertEqual(stat.S_IMODE(env.log_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
