"""SOCKS5 gecit testleri: yerel is parcacikli gecit sunucusu (stdlib).

Sunucu yalnizca CONNECT bilir; istege bagli RFC 1929 kullanici/sifre
dogrulamasi yapar ve hedefe gercek bir TCP baglantisi acip baytlari
iki yone aktarir. Boylece HTTP/FTP motoru gercek bir gecit uzerinden
test edilir (mock degil).
"""
from __future__ import annotations

import hashlib
import json
import os
import select
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ferdim import util
from ferdim.engine import ftp as ftp_mod
from ferdim.engine import httpcli, socks
from ferdim.engine.job import (DownloadJob, JobSpec, JobState, _code_for,
                               _describe_error)
from ferdim.paths import AppEnv
from tests.helpers import DATA_SHA, TINY, start_server
from tests.test_ftp import FtpServer

TINY_SHA = hashlib.sha256(TINY).hexdigest()
FTP_DATA = os.urandom(64 * 1024 + 13)


def wait_state(job, states, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.state in states:
            return True
        time.sleep(0.05)
    return False


def sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _Handler(socketserver.BaseRequestHandler):
    def _recv(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.request.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf.extend(chunk)
        return bytes(buf)

    def _reply(self, rep: int) -> None:
        self.request.sendall(b"\x05" + bytes([rep]) + b"\x00\x01"
                             + socket.inet_aton("127.0.0.1") + b"\x00\x00")

    def handle(self) -> None:
        srv = self.server
        up = None
        try:
            ver, n = self._recv(2)
            if ver != 5:
                return
            methods = set(self._recv(n))
            if srv.user:
                if 0x02 not in methods:
                    self.request.sendall(b"\x05\xff")
                    return
                self.request.sendall(b"\x05\x02")
                ver, ulen = self._recv(2)
                if ver != 1:
                    return
                user = self._recv(ulen).decode("utf-8", "replace")
                plen = self._recv(1)[0]
                password = self._recv(plen).decode("utf-8", "replace")
                ok = user == srv.user and password == srv.password
                with srv.lock:
                    srv.auths.append((user, password, ok))
                self.request.sendall(b"\x01" + (b"\x00" if ok else b"\x01"))
                if not ok:
                    return
            else:
                if 0x00 not in methods:
                    self.request.sendall(b"\x05\xff")
                    return
                self.request.sendall(b"\x05\x00")
            ver, cmd, _rsv, atyp = self._recv(4)
            if ver != 5 or cmd != 1:
                self._reply(7)
                return
            if atyp == 3:
                host = self._recv(self._recv(1)[0]).decode("idna")
            elif atyp == 1:
                host = socket.inet_ntoa(self._recv(4))
            elif atyp == 4:
                host = socket.inet_ntop(socket.AF_INET6, self._recv(16))
            else:
                self._reply(8)
                return
            port = int.from_bytes(self._recv(2), "big")
            with srv.lock:
                srv.requests.append((atyp, host, port))
            if srv.deny:
                self._reply(2)
                return
            try:
                # IPv4 ile coz: test sunuculari 127.0.0.1'e bagli
                addr = socket.getaddrinfo(host, port, socket.AF_INET,
                                          socket.SOCK_STREAM)[0][4]
                up = socket.create_connection(addr, timeout=10)
            except OSError:
                self._reply(4)
                return
            self._reply(0)
            self._pump(up)
        except (EOFError, OSError):
            return
        finally:
            for s in (up, self.request):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def _pump(self, up: socket.socket) -> None:
        peer = self.request
        while not self.server.stopping.is_set():
            try:
                ready, _, _ = select.select([peer, up], [], [], 0.5)
            except OSError:
                return
            for s in ready:
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                other = up if s is peer else peer
                try:
                    other.sendall(data)
                except OSError:
                    return


class SocksServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, user: str = "", password: str = "",
                 deny: bool = False):
        self.user = user
        self.password = password
        self.deny = deny
        self.requests: list[tuple[int, str, int]] = []
        self.auths: list[tuple[str, str, bool]] = []
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        super().__init__(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def url(self, scheme: str = "socks5", user: str = "",
            password: str = "") -> str:
        who = f"{quote(user, safe='')}:{quote(password, safe='')}@" \
            if user else ""
        return f"{scheme}://{who}127.0.0.1:{self.port}"

    def close(self) -> None:
        self.stopping.set()
        self.shutdown()
        self.server_close()


class SocksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http, cls.base = start_server()
        cls.env = AppEnv(root=Path(tempfile.mkdtemp(prefix="ferdim-socks-")))
        cls.cfg = cls.env.load_config()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()

    def setUp(self):
        self._proxies: list[SocksServer] = []

    def tearDown(self):
        for p in self._proxies:
            p.close()

    def proxy(self, **kw) -> SocksServer:
        srv = SocksServer(**kw)
        self._proxies.append(srv)
        return srv

    def _job(self, url, proxy_url, cfg=None, **kw):
        return DownloadJob(self.env, cfg or self.cfg,
                           JobSpec(url=url, proxy=proxy_url, **kw))

    def test_01_http_download_noauth(self):
        """Tunel uzerinden segmentli HTTP indirme; socks5 adi yerelde cozer."""
        srv = self.proxy()
        job = self._job(f"{self.base}/file.bin", srv.url(), segments=8)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR), 60),
                        f"suren durum: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path), DATA_SHA)
        self.assertTrue(srv.requests, "gecit hic CONNECT gormedi")
        # socks5:// hedefi yerelde cozer -> gecide IP gider (ATYP 1)
        self.assertTrue(all(atyp == 1 for atyp, _h, _p in srv.requests))
        self.assertTrue(all(port == self.http.server_address[1]
                            for _a, _h, port in srv.requests))

    def test_02_userpass_auth(self):
        """RFC 1929 kullanici/sifre: URL'deki yuzde kodlamasi cozulur."""
        srv = self.proxy(user="kullanici", password="giz li")
        job = self._job(f"{self.base}/tiny.bin", srv.url(
            user="kullanici", password="giz li"))
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR), 60),
                        f"suren durum: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path), TINY_SHA)
        self.assertTrue(any(ok for _u, _p, ok in srv.auths))

    def test_03_auth_failure_is_auth_code_and_redacted(self):
        """Yanlis sifre: net hata kodu; sifre hata metnine/snapshot'a sizmaz."""
        srv = self.proxy(user="kullanici", password="dogru-sifre")
        cfg = dict(self.cfg)
        cfg["retries"] = 0
        job = self._job(f"{self.base}/tiny.bin",
                        srv.url(user="kullanici", password="yanlis-sifre"),
                        cfg=cfg)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR), 30),
                        f"suren durum: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.ERROR)
        self.assertEqual(job.error_code, "auth")
        self.assertIn("SOCKS5", job.error)
        blob = json.dumps(job.snapshot(), ensure_ascii=False)
        self.assertNotIn("yanlis-sifre", blob)
        self.assertNotIn("dogru-sifre", blob)
        # gecit adresi gosterilirse sifre gizlenir
        self.assertNotIn("yanlis-sifre",
                         util.redact_url(srv.url(user="kullanici",
                                                 password="yanlis-sifre")))

    def test_04_socks5h_remote_resolution(self):
        """socks5h:// hedef adi gecide birakir (yerel DNS sizmaz)."""
        srv = self.proxy()
        url = f"http://localhost:{self.http.server_address[1]}/tiny.bin"
        job = self._job(url, srv.url("socks5h"))
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR), 60),
                        f"suren durum: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertTrue(srv.requests)
        self.assertTrue(all(atyp == 3 and host == "localhost"
                            for atyp, host, _p in srv.requests),
                        srv.requests)

    def test_05_ftp_through_socks(self):
        """FTP kontrol ve pasif veri baglantisi ayni tunelden gecer."""
        ftp = FtpServer({"/dosya.bin": FTP_DATA})
        self.addCleanup(ftp.close)
        srv = self.proxy(user="u", password="p")
        job = self._job(f"{ftp.base}/dosya.bin",
                        srv.url(user="u", password="p"))
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR), 60),
                        f"suren durum: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)
        self.assertEqual(sha(job.final_path),
                         hashlib.sha256(FTP_DATA).hexdigest())
        # kontrol + veri: en az iki tunel
        self.assertGreaterEqual(len(srv.requests), 2)

    def test_06_ftp_without_proxy_still_works(self):
        """Gecitli olmayan FTP yolu eskisi gibi dogrudan calisir (regresyon)."""
        ftp = FtpServer({"/duz.bin": FTP_DATA})
        self.addCleanup(ftp.close)
        job = self._job(f"{ftp.base}/duz.bin", "")
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR), 60),
                        f"suren durum: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.DONE, job.error)

    def test_07_https_wraps_tunnel_with_sni(self):
        """TLS tunel uzerinde kurulur; SNI/dogrulama hedef adiyla yapilir."""
        proxy = "socks5h://127.0.0.1:1080"
        raw, peer = socket.socketpair()
        self.addCleanup(peer.close)

        class Ctx:
            seen = ""

            def wrap_socket(self, sock, server_hostname=None):
                Ctx.seen = server_hostname
                return sock

        with mock.patch.object(socks, "open_tunnel", return_value=raw) as ot:
            conn = httpcli._socks_https_conn(proxy)(
                "ornek.test", 443, timeout=5, context=Ctx())
            conn.connect()
        try:
            self.assertEqual(Ctx.seen, "ornek.test")
            self.assertIs(conn.sock, raw)
            ot.assert_called_once_with(proxy, "ornek.test", 443, 5)
        finally:
            conn.close()
            raw.close()

    def test_08_ftps_data_conn_is_wrapped_over_tunnel(self):
        """FTPS veri kanali tunelden gecen soket uzerinde TLS ile sarilir."""
        proxy = "socks5://127.0.0.1:1080"
        raw, peer = socket.socketpair()
        self.addCleanup(peer.close)

        class Ctx:
            seen = ""

            def wrap_socket(self, sock, server_hostname=None):
                Ctx.seen = server_hostname
                return sock

        ftp = ftp_mod._SocksFTPTLS(proxy, timeout=5)
        ftp.host = "ornek.test"
        ftp.passiveserver = True
        ftp._prot_p = True
        ftp.context = Ctx()
        ftp.makepasv = lambda: ("10.0.0.5", 40000)
        ftp.sendcmd = lambda cmd: "150 aciliyor"
        with mock.patch.object(socks, "open_tunnel", return_value=raw) as ot:
            conn, size = ftp.ntransfercmd("RETR /x.bin")
        try:
            self.assertEqual(Ctx.seen, "ornek.test")
            self.assertIs(conn, raw)
            self.assertIsNone(size)
            # PASV'in ozel adresi degil, kontrol sunucusu hedeflenir
            ot.assert_called_once_with(proxy, "ornek.test", 40000, 5)
        finally:
            ftp.close()
            conn.close()
            raw.close()

    def test_09_invalid_proxy_and_error_mapping(self):
        for bad in ("socks5://", "socks5://127.0.0.1:0",
                    "http://127.0.0.1:1"):
            with self.assertRaises(socks.SocksError) as cm:
                socks.parse(bad)
            self.assertEqual(str(cm.exception), "gecersiz SOCKS5 gecit adresi")
        err = socks.SocksError("SOCKS5 geciti kullanici/sifre kabul etmedi",
                               "auth")
        self.assertEqual(_code_for(err), "auth")
        self.assertEqual(_describe_error(err),
                         "SOCKS5 geciti kullanici/sifre kabul etmedi")
        self.assertTrue(socks.is_socks("SOCKS5H://127.0.0.1:1"))
        self.assertFalse(socks.is_socks("http://127.0.0.1:3128"))
        # Proxy repr'i hata ayiklama ciktisinda sifreyi gostermez
        p = socks.parse("socks5://kullanici:gizli@127.0.0.1:1080")
        self.assertNotIn("gizli", repr(p))
        self.assertEqual(p.host, "127.0.0.1")
        self.assertEqual(p.port, 1080)
        self.assertFalse(p.remote_dns)
        self.assertTrue(socks.parse("socks5h://h").remote_dns)
        self.assertEqual(socks.parse("socks5://h").port, 1080)

    def test_10_denied_connection_is_forbidden(self):
        """Gecit CONNECT'i reddederse (REP 2) hata kodu forbidden olur."""
        srv = self.proxy(deny=True)
        cfg = dict(self.cfg)
        cfg["retries"] = 0
        job = self._job(f"{self.base}/tiny.bin", srv.url(), cfg=cfg)
        job.start()
        self.assertTrue(wait_state(job, (JobState.DONE, JobState.ERROR), 30),
                        f"suren durum: {job.state} {job.error}")
        self.assertEqual(job.state, JobState.ERROR)
        self.assertIn(job.error_code, ("forbidden", "network"))
        self.assertIn("SOCKS5", job.error)


if __name__ == "__main__":
    unittest.main()
