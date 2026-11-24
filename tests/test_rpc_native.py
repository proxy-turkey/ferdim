"""Daemon RPC, HTTP API ve native messaging host uc tan uca testi."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import socketserver
import struct
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ferdim import nativehost
from ferdim.client import FerdimClient
from tests.helpers import DATA, DATA_SHA, start_server, tmp_root


def _env(root: Path) -> dict:
    e = dict(os.environ)
    e.update({
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "PYTHONPATH": str(ROOT),
    })
    return e


class RpcNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = start_server()
        cls.root = tmp_root(cls, "ferdim-rpc-")
        # testler gercek ~/Downloads'a yazmasin
        cfgdir = cls.root / "config" / "ferdim"
        cfgdir.mkdir(parents=True, exist_ok=True)
        (cfgdir / "config.json").write_text(
            json.dumps({"download_dir": str(cls.root / "downloads")}))
        cls.daemon = subprocess.Popen(
            [sys.executable, "-m", "ferdim.daemon"],
            env=_env(cls.root),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # socket hazir olana kadar bekle
        sock = cls.root / "state" / "ferdim" / "ferdim.sock"
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.1)
        cls.sock = str(sock)

    @classmethod
    def tearDownClass(cls):
        cls.daemon.send_signal(signal.SIGTERM)
        try:
            cls.daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.daemon.kill()
        cls.srv.shutdown()

    def _client(self):
        return FerdimClient(self.sock)

    def _job_state(self, c, jid):
        return next((j["state"] for j in c.request("stats")["jobs"]
                     if j["id"] == jid), None)

    def test_00_job_commands_carry_their_id(self):
        """Is kimligiyle cagrilan komutlar (pause/resume/restart/cancel/remove).
        Soket sunucusu 'id' alanini istek kimligi sanip parametreden atiyordu:
        CLI ve GTK'daki is dugmeleri 1.3.0'dan beri sessizce etkisizdi."""
        with self._client() as c:
            r = c.request("add", url=f"{self.base}/slow.bin", segments=2,
                          limit="256K")
            self.assertTrue(r.get("ok"), r)
            jid = r["id"]
            try:
                for _ in range(100):
                    if self._job_state(c, jid) == "downloading":
                        break
                    time.sleep(0.1)
                self.assertEqual(self._job_state(c, jid), "downloading")
                self.assertTrue(c.request("pause", id=jid)["ok"])
                self.assertEqual(self._job_state(c, jid), "paused")
                self.assertTrue(c.request("resume", id=jid)["ok"])
                for _ in range(50):
                    if self._job_state(c, jid) == "downloading":
                        break
                    time.sleep(0.1)
                self.assertEqual(self._job_state(c, jid), "downloading")
                self.assertTrue(c.request("cancel", id=jid)["ok"])
                self.assertEqual(self._job_state(c, jid), "canceled")
                self.assertTrue(c.request("restart", id=jid)["ok"])
                self.assertIn(self._job_state(c, jid), ("queued", "connecting",
                                                        "downloading"))
                # istek kimligi yalniz 'req_id' alanindan gelir ve yankilanir;
                # is kimligi req_id olarak geri donmez
                r = c.request("stats", req_id="istek-42")
                self.assertEqual(r.get("req_id"), "istek-42")
                r = c.request("remove", id=jid)
                self.assertTrue(r["ok"], r)
                self.assertNotIn("req_id", r)
            finally:
                c.request("remove", id=jid)
            self.assertIsNone(self._job_state(c, jid))
            # bilinmeyen is: net hata, sessiz basari degil
            self.assertFalse(c.request("pause", id="boyle-bir-is-yok")["ok"])

    def test_01_ping_version(self):
        with self._client() as c:
            self.assertTrue(c.request("ping")["ok"])
            self.assertIn("version", c.request("version"))

    def test_02_add_and_complete(self):
        with self._client() as c:
            r = c.request("add", url=f"{self.base}/file.bin", segments=8)
            self.assertTrue(r["ok"], r)
            jid = r["id"]
            path = None
            for _ in range(200):
                st = c.request("stats")
                job = next(j for j in st["jobs"] if j["id"] == jid)
                if job["state"] in ("done", "error", "canceled"):
                    path = job["fullpath"]
                    self.assertEqual(job["state"], "done", job.get("error"))
                    break
                time.sleep(0.15)
            self.assertIsNotNone(path)
            self.assertEqual(
                hashlib.sha256(Path(path).read_bytes()).hexdigest(), DATA_SHA)

    def test_03_http_api_and_web(self):
        root = self.root / "state" / "ferdim"
        token = (root / "token").read_text().strip()
        port = (root / "port").read_text().strip()
        # 1.7.1 yukseltmesi: ilk acilista token yenilendi, isaret yazildi
        self.assertTrue((root / "token.v2").is_file())

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/stats",
            headers={"X-FerDim-Token": token})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 200)
            self.assertIn(b'"jobs"', r.read())

        # token'siz istek reddedilmeli
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stats")
            self.fail("401 bekleniyordu")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

        # ana sayfa token GOMMEZ: tarayici tek kullanimlik biletle acilir
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
            html = r.read().decode()
        self.assertNotIn(token, html)
        self.assertNotIn("__TOKEN__", html)
        self.assertNotIn("FERDIM_TOKEN", html)

        # `ferdim web` akisi: soketten bilet, HTTP'de tek kullanimlik takas
        with self._client() as c:
            r = c.request("web-session")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["url"].split("/#s=")[0], f"http://127.0.0.1:{port}")
        nonce = r["url"].split("#s=", 1)[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/session",
            data=json.dumps({"nonce": nonce}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            minted = json.loads(resp.read())["token"]
        self.assertEqual(minted, token)          # bilet gercek token'i verdi
        # bilet tek kullanimlik: ikinci takas 403
        try:
            urllib.request.urlopen(req)
            self.fail("403 bekleniyordu")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

        # token donusumu kosan daemon'da aninda gecerli olur
        with self._client() as c:
            self.assertTrue(c.request("token-rotate")["ok"])
        new = (root / "token").read_text().strip()
        self.assertNotEqual(new, token)
        old = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/stats",
            headers={"X-FerDim-Token": token})
        try:
            urllib.request.urlopen(old)
            self.fail("eski token 401 almaliydi")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/stats",
            headers={"X-FerDim-Token": new})
        with urllib.request.urlopen(req) as r:
            self.assertIn(b'"jobs"', r.read())
        # sonraki testler (native host) token dosyasini okumaz, ama daemon'in
        # token'i artik yeni degerdir; testleri etkilemez.

    def test_04_native_host_protocol(self):
        p = subprocess.Popen(
            [sys.executable, "-m", "ferdim.nativehost"],
            env=_env(self.root),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)

        def send(obj):
            data = json.dumps(obj).encode()
            p.stdin.write(struct.pack("<I", len(data)) + data)
            p.stdin.flush()

        def recv():
            raw = p.stdout.read(4)
            (n,) = struct.unpack("<I", raw)
            return json.loads(p.stdout.read(n))

        send({"cmd": "ping"})
        r = recv()
        self.assertTrue(r["ok"], r)

        send({"cmd": "download", "url": f"{self.base}/norange.bin",
              "filename": "nm-test.bin"})
        r = recv()
        self.assertTrue(r["ok"], r)

        with self._client() as c:
            for _ in range(150):
                st = c.request("stats")
                jobs = [j for j in st["jobs"] if j["filename"] == "nm-test.bin"]
                if jobs and jobs[0]["state"] == "done":
                    self.assertEqual(
                        hashlib.sha256(
                            Path(jobs[0]["fullpath"]).read_bytes()).hexdigest(),
                        hashlib.sha256(DATA[:1024 * 1024]).hexdigest())
                    break
                time.sleep(0.15)
            else:
                self.fail("native host indirmesi tamamlanmadi")

        p.stdin.close()
        p.wait(timeout=10)


class _Recorder(socketserver.ThreadingUnixStreamServer):
    """Soket protokolunu taklit eden kayit sunucusu (gercek kuyruk yok)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path, seen):
        self.seen = seen
        super().__init__(str(path), _RecordHandler)


class _RecordHandler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            try:
                msg = json.loads(line)
            except Exception:
                continue
            self.server.seen.append(msg)
            self.wfile.write(b'{"ok": true}\n')
            self.wfile.flush()


class NativeHostFilterTests(unittest.TestCase):
    """Eklenti cerceveleri: toplu indirme alanlari filtrelenir, nesne
    olmayan JSON cercevesi hata yaniti alir (surec devrilmez)."""

    @classmethod
    def setUpClass(cls):
        cls.root = tmp_root(cls, "ferdim-nm-")
        cfgdir = cls.root / "config" / "ferdim"
        cfgdir.mkdir(parents=True, exist_ok=True)
        (cfgdir / "config.json").write_text(
            json.dumps({"download_dir": str(cls.root / "dl")}))
        cls.sock = cls.root / "state" / "ferdim" / "ferdim.sock"
        cls.sock.parent.mkdir(parents=True, exist_ok=True)
        cls.seen = []
        cls.srv = _Recorder(cls.sock, cls.seen)
        cls.addClassCleanup(cls.srv.server_close)
        cls.addClassCleanup(cls.srv.shutdown)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    def _host(self):
        p = subprocess.Popen(
            [sys.executable, "-m", "ferdim.nativehost"],
            env=_env(self.root),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        # LIFO: once borular kapanir, en son bekle
        self.addCleanup(p.wait, 10)
        self.addCleanup(p.stdin.close)
        self.addCleanup(p.stdout.close)
        return p

    @staticmethod
    def _send(p, obj):
        data = json.dumps(obj).encode()
        p.stdin.write(struct.pack("<I", len(data)) + data)
        p.stdin.flush()

    @staticmethod
    def _recv(p):
        raw = p.stdout.read(4)
        if len(raw) != 4:
            raise AssertionError("native host yanit vermeden kapandi")
        (n,) = struct.unpack("<I", raw)
        return json.loads(p.stdout.read(n))

    def test_01_batch_and_download_only_forward_whitelisted_keys(self):
        p = self._host()
        start = len(self.seen)
        self._send(p, {"cmd": "download-batch", "items": [
            {"url": "http://127.0.0.1:1/a.bin", "filename": "a.bin",
             "referer": "http://sayfa/", "cookies": "k=v", "ua": "UA",
             "segments": 4, "merkle": "/etc/passwd",
             "proxy": "socks5://127.0.0.1:9", "insecure": True,
             "headers": ["X: y"], "checksum": "sha256:" + "0" * 64,
             "ephemeral": True},
            "bozuk-oge",                       # dict degil: atlanir
            {"url": "http://127.0.0.1:1/b.bin", "filename": "b.bin"},
        ]})
        r = self._recv(p)
        self.assertTrue(r["ok"], r)
        batch = next(m for m in self.seen[start:]
                     if m.get("cmd") == "add-batch")
        items = batch["items"]
        self.assertEqual([it["filename"] for it in items],
                         ["a.bin", "b.bin"])
        allowed = set(nativehost.DOWNLOAD_DEFAULTS)
        self.assertNotIn("dir", allowed)  # tarayici hedef klasor secemez
        for it in items:
            self.assertLessEqual(set(it), allowed)
        for banned in ("merkle", "proxy", "insecure", "headers", "checksum"):
            self.assertNotIn(banned, items[0])
        # ozel pencere isareti gecer; isaretsiz ogede alan yok (varsayilan False)
        self.assertIs(items[0]["ephemeral"], True)
        self.assertNotIn("ephemeral", items[1])

        # tek 'download' ayni kumeyi kullanir (parite)
        start = len(self.seen)
        self._send(p, {"cmd": "download", "url": "http://127.0.0.1:1/c.bin",
                       "filename": "c.bin", "merkle": "/etc/passwd",
                       "dir": "/tmp/baska", "proxy": "socks5://127.0.0.1:9",
                       "insecure": True, "ephemeral": True})
        self.assertTrue(self._recv(p)["ok"])
        add = next(m for m in self.seen[start:] if m.get("cmd") == "add")
        self.assertLessEqual(set(add) - {"cmd"}, allowed)
        self.assertNotIn("merkle", add)
        self.assertNotIn("dir", add)
        self.assertIs(add["ephemeral"], True)

    def test_02_non_dict_frames_answer_with_error(self):
        p = self._host()
        for frame in ([1, 2], "metin", None):
            with self.subTest(frame=frame):
                self._send(p, frame)
                r = self._recv(p)
                self.assertFalse(r["ok"], r)
                self.assertIn("gecersiz", r["error"])
        # surec ayakta: sonraki komut normal yanitlanir
        self._send(p, {"cmd": "ping"})
        r = self._recv(p)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["inReplyTo"], "ping")


if __name__ == "__main__":
    unittest.main(verbosity=2)
