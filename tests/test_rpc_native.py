"""Daemon RPC, HTTP API ve native messaging host uc tan uca testi."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ferdim.client import FerdimClient
from tests.helpers import DATA, DATA_SHA, start_server


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
        cls.root = Path(tempfile.mkdtemp(prefix="ferdim-rpc-"))
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
            r = c.request("add", url=f"{self.base}/slow.bin", segments=2)
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

        # ana sayfa token'i gomuluyor
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
            html = r.read().decode()
        self.assertIn(token, html)
        self.assertNotIn("__TOKEN__", html)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
