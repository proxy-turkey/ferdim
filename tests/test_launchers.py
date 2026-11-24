"""Web arayuzu baslaticilari: tek kullanimlik oturum URL'si.

CLI (`ferdim web`), native messaging host (`web-url`) ve D-Bus/GNOME
katmanlari API jetonunu tasimaz: adres daemon'dan `web-session` ile
aliniyor. Burada sahte soket sunucusu kullanilir; gercek HTTP sunucusu ve
tarayici gerekmez.
"""
from __future__ import annotations

import json
import os
import shutil
import socketserver
import struct
import subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ferdim import nativehost
from tests.helpers import tmp_root

URL = "http://127.0.0.1:8378/#s=tek-kullanimlik"


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        state = self.server.state
        for line in self.rfile:
            try:
                msg = json.loads(line)
            except Exception:
                continue
            state.seen.append(msg)
            cmd = msg.get("cmd")
            if cmd == "web-session":
                resp = dict(state.web)
            elif cmd == "token-rotate":
                resp = dict(state.rotate)
            elif cmd == "add":
                resp = {"ok": True, "id": "j-1", "filename": "x.bin"}
            elif cmd == "add-batch":
                resp = {"ok": True, "ids": []}
            else:
                resp = {"ok": True}
            self.wfile.write((json.dumps(resp) + "\n").encode())
            self.wfile.flush()


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, state):
        self.state = state
        super().__init__(path, _Handler)


class FakeDaemon:
    """Soket protokolunu taklit eden sunucu (gercek kuyruk yok)."""

    def __init__(self, case, web=None, rotate=None):
        self.root = tmp_root(case, "ferdim-launch-")
        self.state = type("S", (), {
            "seen": [],
            "web": web or {"ok": True, "url": URL},
            "rotate": rotate or {"ok": True},
        })()
        sock = self.root / "state" / "ferdim" / "ferdim.sock"
        sock.parent.mkdir(parents=True, exist_ok=True)
        self.sock = sock
        self.srv = _Server(str(sock), self.state)
        case.addCleanup(self.srv.server_close)
        case.addCleanup(self.srv.shutdown)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def commands(self):
        return [m.get("cmd") for m in self.state.seen]

    def env(self) -> dict:
        # PATH: xdg-open yok; CLI tarayici baslatmaya calismasin.
        bindir = self.root / "bin"
        bindir.mkdir(exist_ok=True)
        e = dict(os.environ)
        e.update({
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "PYTHONPATH": str(ROOT),
            "PATH": str(bindir),
        })
        return e

    def cli(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ferdim.cli", *args],
            env=self.env(), cwd=str(ROOT), text=True,
            capture_output=True, timeout=60)


class CliWebSessionTests(unittest.TestCase):
    def test_web_prints_the_fresh_session_url(self):
        d = FakeDaemon(self)
        proc = d.cli("web")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), URL)
        self.assertIn("web-session", d.commands())

    def test_web_disabled_exits_non_zero_without_url(self):
        d = FakeDaemon(self, web={"ok": False, "code": "web_disabled"})
        proc = d.cli("web")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertTrue(proc.stderr.strip())

    def test_token_rotate_reports_success(self):
        d = FakeDaemon(self)
        proc = d.cli("token", "rotate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip())
        self.assertEqual(d.commands().count("token-rotate"), 1)

    def test_token_rotate_failure_exits_non_zero(self):
        d = FakeDaemon(self, rotate={"ok": False, "code": "nope"})
        proc = d.cli("token", "rotate")
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(proc.stderr.strip())


class NativeHostWebUrlTests(unittest.TestCase):
    def _host(self, d):
        p = subprocess.Popen(
            [sys.executable, "-m", "ferdim.nativehost"], env=d.env(),
            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
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

    def test_web_url_returns_session_url(self):
        d = FakeDaemon(self)
        p = self._host(d)
        self._send(p, {"cmd": "web-url"})
        r = self._recv(p)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r.get("url"), URL)
        self.assertEqual(r.get("inReplyTo"), "web-url")
        self.assertIn("web-session", d.commands())

    def test_download_folder_cannot_be_set_from_the_browser(self):
        self.assertNotIn("dir", nativehost.DOWNLOAD_DEFAULTS)
        d = FakeDaemon(self)
        p = self._host(d)
        self._send(p, {"cmd": "download", "url": "http://127.0.0.1:1/x.bin",
                       "dir": "/tmp/baska-klasor"})
        self.assertTrue(self._recv(p).get("ok"))
        add = next(m for m in d.state.seen if m.get("cmd") == "add")
        self.assertNotIn("dir", add)
        self.assertLessEqual(set(add) - {"cmd"},
                             set(nativehost.DOWNLOAD_DEFAULTS))


class LauncherSourceTests(unittest.TestCase):
    GNOME_JS = (ROOT / "gnome-shell-extension" /
                "ferdim@proxy-turkey.github.io" / "extension.js")
    POPUP_JS = ROOT / "extension" / "popup.js"

    def test_gnome_extension_asks_the_bridge(self):
        src = self.GNOME_JS.read_text("utf-8")
        self.assertIn("WebUrl", src)
        self.assertIn("launch_default_for_uri", src)
        self.assertNotIn("file_get_contents", src)
        self.assertNotIn("127.0.0.1:${port}", src)

    @unittest.skipUnless(shutil.which("node"), "node yok")
    def test_popup_parses_with_node(self):
        proc = subprocess.run([shutil.which("node"), "--check",
                               str(self.POPUP_JS)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_popup_requests_a_fresh_url(self):
        src = self.POPUP_JS.read_text("utf-8")
        self.assertIn('sendNativeMessage(NATIVE, { cmd: "web-url" })', src)
        self.assertIn("browser.tabs.create", src)
        self.assertIn("webUiUnavailable", src)
        self.assertNotIn('"http://127.0.0.1:" +', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
