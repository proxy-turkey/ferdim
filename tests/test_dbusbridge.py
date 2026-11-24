"""org.ferdim.Daemon1 koprusu: esleme + (varsa) gercek D-Bus uctan uca."""
from __future__ import annotations

import json
import shlex
import shutil
import socketserver
import subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ferdim import __version__
from ferdim import dbusbridge
from tests.helpers import tmp_root

try:
    from gi.repository import Gio
    HAVE_GI = True
except Exception:
    HAVE_GI = False


class FakeClient:
    def __init__(self, response=None):
        self.response = response or {"ok": True, "id": "j-1"}
        self.calls: list[tuple[str, dict]] = []

    def request(self, cmd, **params):
        self.calls.append((cmd, params))
        return self.response


class BridgeMappingTests(unittest.TestCase):
    def test_add_maps_to_add_command(self):
        client = FakeClient({"ok": True, "id": "is-42"})
        bridge = dbusbridge.Bridge(client)
        self.assertEqual(bridge.call("Add", "https://example.test/a.iso"),
                         "is-42")
        self.assertEqual(client.calls, [("add", {"url":
                                                  "https://example.test/a.iso"})])

    def test_control_methods_map_to_job_commands(self):
        client = FakeClient({"ok": True})
        bridge = dbusbridge.Bridge(client)
        for method, cmd in (("Pause", "pause"), ("Resume", "resume"),
                            ("Cancel", "cancel")):
            self.assertIsNone(bridge.call(method, "is-7"))
            self.assertEqual(client.calls[-1], (cmd, {"id": "is-7"}))

    def test_list_and_stats_return_json(self):
        client = FakeClient({"ok": True, "jobs": [], "total_speed": 0})
        bridge = dbusbridge.Bridge(client)
        payload = bridge.call("List")
        self.assertEqual(json.loads(payload)["jobs"], [])
        self.assertEqual(client.calls[0][0], "list")
        payload = bridge.call("Stats")
        self.assertEqual(json.loads(payload)["total_speed"], 0)
        self.assertEqual(client.calls[1][0], "stats")

    def test_daemon_error_becomes_bridge_error(self):
        bridge = dbusbridge.Bridge(FakeClient({"ok": False,
                                               "error": "is yok"}))
        with self.assertRaises(dbusbridge.BridgeError):
            bridge.call("Pause", "missing")

    def test_reconnects_once_after_daemon_restart(self):
        class FlakyClient(FakeClient):
            def __init__(self):
                super().__init__({"ok": True, "jobs": []})
                self.closed = 0

            def request(self, cmd, **params):
                if not self.calls:
                    self.calls.append((cmd, params))
                    raise ConnectionError("daemon gitti")
                return super().request(cmd, **params)

            def close(self):
                self.closed += 1

        client = FlakyClient()
        bridge = dbusbridge.Bridge(client)
        self.assertIn("jobs", bridge.call("List"))
        self.assertEqual(client.closed, 1)
        self.assertEqual(client.calls[-1][0], "list")

    def test_web_url_maps_to_web_session(self):
        client = FakeClient({"ok": True,
                             "url": "http://127.0.0.1:8378/#s=abc"})
        bridge = dbusbridge.Bridge(client)
        self.assertEqual(bridge.call("WebUrl"),
                         "http://127.0.0.1:8378/#s=abc")
        self.assertEqual(client.calls, [("web-session", {})])

    def test_web_url_disabled_becomes_bridge_error(self):
        bridge = dbusbridge.Bridge(
            FakeClient({"ok": False, "code": "web_disabled"}))
        with self.assertRaises(dbusbridge.BridgeError) as ctx:
            bridge.call("WebUrl")
        self.assertIn("web_disabled", str(ctx.exception))

    def test_web_url_without_address_is_an_error(self):
        bridge = dbusbridge.Bridge(FakeClient({"ok": True}))
        with self.assertRaises(dbusbridge.BridgeError):
            bridge.call("WebUrl")

    def test_unknown_method_rejected(self):
        with self.assertRaises(dbusbridge.BridgeError):
            dbusbridge.client_command("Explode")

    def test_empty_job_id_rejected(self):
        with self.assertRaises(dbusbridge.BridgeError):
            dbusbridge.client_command("Pause", [""])

    def test_introspection_xml_covers_interface(self):
        xml = dbusbridge.introspection_xml()
        for token in ("org.ferdim.Daemon1", "Add", "Pause", "Resume",
                      "Cancel", "List", "Stats", "WebUrl", "Changed",
                      "Version"):
            self.assertIn(token, xml)
        if HAVE_GI:
            node = Gio.DBusNodeInfo.new_for_xml(xml)
            self.assertEqual(node.interfaces[0].name,
                             "org.ferdim.Daemon1")

    def test_embedded_xml_matches_data_file(self):
        disk = (ROOT / "data" / "org.ferdim.Daemon1.xml").read_text("utf-8")
        self.assertEqual(dbusbridge.introspection_xml([]).strip(),
                         disk.strip())

    def test_missing_xml_files_fall_back_to_embedded_copy(self):
        xml = dbusbridge.introspection_xml([Path("/nonexistent/daemon.xml")])
        self.assertIn("org.ferdim.Daemon1", xml)
        if HAVE_GI:
            Gio.DBusNodeInfo.new_for_xml(xml)


class DesktopIntegrationTests(unittest.TestCase):
    EXT = (ROOT / "gnome-shell-extension" /
           "ferdim@proxy-turkey.github.io" / "extension.js")

    def test_extension_menu_rebuild_ignores_speed(self):
        src = self.EXT.read_text("utf-8")
        self.assertNotIn("j.id, j.state, j.filename, j.speed", src)
        self.assertIn("this.menu.isOpen", src)
        self.assertIn("this._menuDirty", src)

    def test_install_script_installs_and_removes_dbus_xml(self):
        src = (ROOT / "scripts" / "install.sh").read_text("utf-8")
        self.assertIn("dbus-1/interfaces", src)
        self.assertIn("org.ferdim.Daemon1.xml", src)
        self.assertIn('rm -f "$DBUS_IFACE/org.ferdim.Daemon1.xml"', src)


class _FakeDaemonHandler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            try:
                msg = json.loads(line)
            except Exception:
                return
            if msg.get("cmd") == "watch":
                continue              # baglantiyi acik tut
            resp = {"ok": True, "req_id": msg.get("req_id")}
            if msg.get("cmd") in ("list", "stats"):
                resp.update({"jobs": [], "total_speed": 0})
            elif msg.get("cmd") == "web-session":
                resp["url"] = "http://127.0.0.1:8378/#s=e2e"
            self.wfile.write((json.dumps(resp) + "\n").encode())
            self.wfile.flush()


@unittest.skipUnless(HAVE_GI, "python3-gobject (gi) yok")
@unittest.skipUnless(shutil.which("dbus-run-session") and
                     shutil.which("gdbus"),
                     "dbus-run-session/gdbus yok")
class BridgeEndToEndTests(unittest.TestCase):
    def test_gdbus_call_through_real_bus(self):
        root = tmp_root(self, "ferdim-dbus-")
        state = root / "state" / "ferdim"
        state.mkdir(parents=True)
        sock = state / "ferdim.sock"

        srv = socketserver.ThreadingUnixStreamServer(
            str(sock), _FakeDaemonHandler)
        srv.daemon_threads = True
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        script = "\n".join([
            "set -eu",
            "export PYTHONPATH=%s" % shlex.quote(str(ROOT)),
            "export XDG_STATE_HOME=%s" % shlex.quote(str(root / "state")),
            "export XDG_CONFIG_HOME=%s" % shlex.quote(str(root / "config")),
            "%s -m ferdim.dbusbridge &" % shlex.quote(sys.executable),
            "pid=$!",
            "trap 'kill \"$pid\" 2>/dev/null || true' EXIT",
            "for i in $(seq 1 100); do",
            "  if gdbus call --session --dest %s --object-path %s"
            " --method org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1;"
            " then break; fi" % (dbusbridge.BUS_NAME, dbusbridge.OBJECT_PATH),
            "  sleep 0.1",
            "done",
            "gdbus call --session --dest %s --object-path %s"
            " --method %s.List" % (dbusbridge.BUS_NAME,
                                   dbusbridge.OBJECT_PATH,
                                   dbusbridge.INTERFACE),
            "gdbus call --session --dest %s --object-path %s"
            " --method %s.WebUrl" % (dbusbridge.BUS_NAME,
                                     dbusbridge.OBJECT_PATH,
                                     dbusbridge.INTERFACE),
            "gdbus call --session --dest %s --object-path %s"
            " --method org.freedesktop.DBus.Properties.Get %s Version"
            % (dbusbridge.BUS_NAME, dbusbridge.OBJECT_PATH,
               dbusbridge.INTERFACE),
        ])
        proc = subprocess.run(
            ["dbus-run-session", "--", "bash", "-c", script],
            cwd=str(ROOT), text=True, capture_output=True, timeout=60)
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("jobs", proc.stdout)
        self.assertIn(__version__, proc.stdout)
        self.assertIn("http://127.0.0.1:8378/#s=e2e", proc.stdout)


if __name__ == "__main__":
    unittest.main()
