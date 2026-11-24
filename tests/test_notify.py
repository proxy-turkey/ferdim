"""Masaustu bildirimleri: esleme, birlestirme, tasiyici secimi, no-op."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import unittest
from unittest import mock

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ferdim.notify import Notifier, _compose
from ferdim import notify as notify_mod
from ferdim.api import _sanitize_config
from tests.helpers import tmp_root


def _snap(jid="j1", state="done", chk="", error="", code="",
          size=2048, name="dosya.bin"):
    return {"id": jid, "state": state, "checksum": chk,
            "filename": name, "size": size, "error": error,
            "error_code": code, "url": "http://example.test/" + name}


def _cfg(**kw):
    cfg = {"language": "en", "notify": True, "notify_errors_only": False}
    cfg.update(kw)
    return cfg


class FakeRun:
    """subprocess.run yerine: komutlari kaydeder, istenen sonucu dondurur."""

    def __init__(self, success=True, only=None, event=None):
        self.cmds: list[list[str]] = []
        self.success = success
        self.only = only
        self.event = event

    def __call__(self, cmd):
        self.cmds.append(list(cmd))
        if self.event is not None:
            self.event.set()
        if self.only is not None:
            return cmd[0] == self.only
        return self.success

    def text(self) -> str:
        return " ".join(" ".join(c) for c in self.cmds)


class NotifyComposeTests(unittest.TestCase):
    def test_single_done_names_checksum(self):
        title, body = _compose([Notifier._event(
            _snap(chk="ok"), "done")], "en")
        self.assertIn("complete", title.lower())
        self.assertIn("dosya.bin", body)
        self.assertIn("checksum verified", body)

    def test_single_error_uses_error_code(self):
        ev = Notifier._event(
            _snap(state="error", code="not_found", error="ham"), "error")
        title, body = _compose([ev], "en")
        self.assertIn("failed", title.lower())
        self.assertIn("not found", body.lower())

    def test_burst_becomes_one_summary(self):
        events = [Notifier._event(_snap(jid=str(i), chk="warn"), "done")
                  for i in range(3)]
        title, body = _compose(events, "en")
        self.assertIn("3 downloads", title)
        self.assertIn("3 completed", body)
        self.assertIn("checksum warning", body)


class NotifyTransportTests(unittest.TestCase):
    def test_missing_command_is_not_an_error(self):
        with mock.patch("ferdim.notify._child_env", return_value={}):
            with mock.patch("ferdim.notify.subprocess.run",
                            side_effect=FileNotFoundError):
                self.assertFalse(notify_mod._run(["gdbus"]))
            with mock.patch("ferdim.notify.subprocess.run",
                            side_effect=subprocess.TimeoutExpired("x", 1)):
                self.assertFalse(notify_mod._run(["gdbus"]))

    def test_successful_command_returns_true(self):
        done = subprocess.CompletedProcess(["x"], 0)
        with mock.patch("ferdim.notify._child_env", return_value={}):
            with mock.patch("ferdim.notify.subprocess.run",
                            return_value=done) as run:
                self.assertTrue(notify_mod._run(["gdbus"]))
        self.assertIn("env", run.call_args.kwargs)

    def test_freedesktop_quotes_every_string_argument(self):
        run = FakeRun()
        notify_mod._freedesktop(run, "FerDim finished", "file name (2 MB)")
        args = run.cmds[0]
        tail = args[args.index("org.freedesktop.Notifications.Notify") + 1:]
        self.assertEqual(tail[0], "'FerDim'")
        self.assertEqual(tail[1], "uint32 0")
        self.assertEqual(tail[2], "'ferdim'")
        self.assertEqual(tail[3], "'FerDim finished'")
        self.assertEqual(tail[4], "'file name (2 MB)'")
        self.assertEqual(tail[5:], ["@as []", "@a{sv} {}", "int32 5000"])

    def test_portal_payload_is_gvariant_quoted(self):
        run = FakeRun()
        notify_mod._portal(run, "FerDim's job", "line 1")
        payload = run.cmds[0][-1]
        self.assertIn("<'FerDim\\'s job'>", payload)
        self.assertIn("<'line 1'>", payload)

    def test_runtime_dir_bus_socket_is_accepted(self):
        root = tmp_root(self, "ferdim-notify-")
        bus = root / "bus"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(bus))
        self.addCleanup(sock.close)
        with mock.patch.dict(os.environ,
                             {"DBUS_SESSION_BUS_ADDRESS": "",
                              "XDG_RUNTIME_DIR": str(root)}):
            self.assertTrue(notify_mod._bus_available())
            env = notify_mod._child_env()
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"],
                         "unix:path=" + str(bus))

    @unittest.skipUnless(shutil.which("gdbus") and
                         shutil.which("dbus-run-session"),
                         "gdbus/dbus-run-session yok")
    def test_gdbus_parses_freedesktop_arguments(self):
        run = FakeRun()
        notify_mod._freedesktop(run, "FerDim finished",
                                "dosya adi (2.0 MB) — tamam")
        proc = subprocess.run(["dbus-run-session", "--"] + run.cmds[0],
                              capture_output=True, text=True, timeout=30)
        output = (proc.stdout + proc.stderr).lower()
        self.assertNotIn("error parsing", output)
        self.assertNotIn("cannot parse", output)


class NotifierTests(unittest.TestCase):
    def test_config_defaults_and_string_false(self):
        cfg = {}
        _sanitize_config(cfg)
        self.assertIs(cfg["notify"], True)
        self.assertIs(cfg["notify_errors_only"], False)
        cfg = {"notify": "false", "notify_errors_only": "yes"}
        _sanitize_config(cfg)
        self.assertIs(cfg["notify"], False)
        self.assertIs(cfg["notify_errors_only"], True)

    def _notifier(self, run, cfg=None, bus=True, **kw):
        n = Notifier(cfg or _cfg(), run=run,
                     bus_available=lambda: bus, **kw)
        self.addCleanup(n.stop)
        return n

    def test_disabled_config_sends_nothing(self):
        run = FakeRun()
        n = self._notifier(run, _cfg(notify=False))
        n.on_job(_snap(state="queued"))
        n.on_job(_snap(state="done", chk="ok"))
        self.assertFalse(n._drain())
        self.assertEqual(run.cmds, [])

    def test_initial_done_state_is_suppressed(self):
        run = FakeRun()
        n = self._notifier(run)
        n.on_job(_snap(state="done", chk="ok"))   # daemon acilisinda hazir is
        self.assertFalse(n._drain())
        self.assertEqual(run.cmds, [])

    def test_done_transition_sends_portal_notification(self):
        run = FakeRun(success=True)
        n = self._notifier(run)
        n.on_job(_snap(state="downloading"))
        n.on_job(_snap(state="done", chk="ok"))
        self.assertTrue(n._drain())
        self.assertEqual(len(run.cmds), 1)
        self.assertIn("org.freedesktop.portal.Notification.AddNotification",
                      run.cmds[0])
        self.assertIn("dosya.bin", run.text())
        self.assertIn("checksum verified", run.text())

    def test_burst_is_coalesced_into_one_notification(self):
        run = FakeRun(success=True)
        n = self._notifier(run)
        for jid in ("a", "b", "c"):
            n.on_job(_snap(jid=jid, state="queued"))
            n.on_job(_snap(jid=jid, state="done", chk="ok"))
        n._drain()
        self.assertEqual(len(run.cmds), 1, run.cmds)
        self.assertIn("3 downloads finished", run.text())

    def test_errors_only_skips_completion(self):
        run = FakeRun(success=True)
        n = self._notifier(run, _cfg(notify_errors_only=True))
        n.on_job(_snap(state="queued"))
        n.on_job(_snap(state="done", chk="ok"))
        self.assertFalse(n._drain())
        n.on_job(_snap(state="error", code="network", error="x"))
        self.assertTrue(n._drain())
        self.assertEqual(len(run.cmds), 1)
        self.assertIn("Download failed", run.text())

    def test_late_verification_after_done(self):
        run = FakeRun(success=True)
        n = self._notifier(run)
        n.on_job(_snap(state="queued"))
        n.on_job(_snap(state="done", chk=""))
        n._drain()                       # bitis bildirimi
        n.on_job(_snap(state="done", chk="warn"))
        self.assertTrue(n._drain())
        self.assertEqual(len(run.cmds), 2)
        self.assertIn("Verification result", run.text())
        self.assertIn("sidecar checksum mismatch", run.text())

    def test_transport_fallback_order(self):
        run = FakeRun(only="notify-send")
        n = self._notifier(run)
        n.on_job(_snap(state="queued"))
        n.on_job(_snap(state="done"))
        n._drain()
        self.assertEqual([c[0] for c in run.cmds],
                         ["gdbus", "gdbus", "notify-send"])

    def test_no_session_bus_is_silent(self):
        run = FakeRun()
        n = self._notifier(run, bus=False)
        n.on_job(_snap(state="queued"))
        n.on_job(_snap(state="done"))
        n._drain()
        self.assertEqual(run.cmds, [])

    def test_default_bus_check_reads_environment(self):
        run = FakeRun()
        n = Notifier(_cfg(), run=run)
        self.addCleanup(n.stop)
        root = tmp_root(self, "ferdim-notify-")
        with mock.patch.dict(os.environ,
                             {"DBUS_SESSION_BUS_ADDRESS": "",
                              "XDG_RUNTIME_DIR": str(root)}):
            n.on_job(_snap(state="queued"))
            n.on_job(_snap(state="done"))
            n._drain()
        self.assertEqual(run.cmds, [])

    def test_missing_bus_logs_once(self):
        run = FakeRun()
        n = self._notifier(run, bus=False)
        with mock.patch.object(notify_mod.log, "info") as info:
            for jid in ("a", "b"):
                n.on_job(_snap(jid=jid, state="queued"))
                n.on_job(_snap(jid=jid, state="done"))
                n._drain()
        self.assertEqual(info.call_count, 1)
        self.assertEqual(run.cmds, [])

    def test_background_worker_delivers(self):
        sent = threading.Event()
        run = FakeRun(success=True, event=sent)
        n = Notifier(_cfg(), run=run, bus_available=lambda: True,
                     coalesce=0.01, min_interval=0.0).start()
        self.addCleanup(n.stop)
        n.on_job(_snap(state="queued"))
        n.on_job(_snap(state="done", chk="ok"))
        self.assertTrue(sent.wait(2.0), "bildirim iscisi calismadi")
        self.assertGreaterEqual(len(run.cmds), 1)


if __name__ == "__main__":
    unittest.main()
