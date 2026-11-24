"""org.ferdim.Daemon1 D-Bus koprusu (opsiyonel PyGObject/Gio runtime).

Cekirdek stdlib kalir: bu modul yalnizca masaustu kabuklari icin ayri bir
surectir (GTK arayuzu gibi). gi yoksa main() anlasilir hata verir; import
sirasinda patlamaz, boylece testler ve salt-CLI kurulumlar etkilenmez.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .client import FerdimClient
from .paths import AppEnv

try:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
except Exception:                       # gi kurulu degil: salt-CLI ortam
    Gio = GLib = None

BUS_NAME = "org.ferdim.Daemon"
OBJECT_PATH = "/org/ferdim/Daemon"
INTERFACE = "org.ferdim.Daemon1"
XML_FILE = "org.ferdim.Daemon1.xml"


class BridgeError(Exception):
    pass


def client_command(method: str, args=()) -> tuple[str, dict]:
    """D-Bus yontemi -> daemon komutu + parametreler (saf esleme)."""
    args = list(args or ())
    if method == "Add":
        url = args[0] if args else ""
        if not isinstance(url, str) or not url.strip():
            raise BridgeError("Add: URL gerekli")
        return "add", {"url": url}
    if method in ("Pause", "Resume", "Cancel"):
        jid = args[0] if args else ""
        if not isinstance(jid, str) or not jid:
            raise BridgeError("%s: is kimligi gerekli" % method)
        return method.lower(), {"id": jid}
    if method == "List":
        return "list", {}
    if method == "Stats":
        return "stats", {}
    raise BridgeError("bilinmeyen yontem: %s" % method)


class Bridge:
    """D-Bus cagrilarini unix socket istemcisine tasir."""

    def __init__(self, client: FerdimClient):
        self.client = client

    def call(self, method: str, *args):
        cmd, params = client_command(method, args)
        try:
            resp = self.client.request(cmd, **params)
        except (ConnectionError, OSError):
            # daemon yeniden basladi: eski soketi birakip bir kez dene
            self.client.close()
            resp = self.client.request(cmd, **params)
        if not resp.get("ok"):
            raise BridgeError(str(resp.get("error") or "ferdim hatasi"))
        if method == "Add":
            return str(resp.get("id") or "")
        if method in ("List", "Stats"):
            return json.dumps(resp, ensure_ascii=False)
        return None


def introspection_xml() -> str:
    """Arayuz XML'i: depo 'data/' dizini ya da kurulu standart yollar."""
    here = Path(__file__).resolve().parent
    data_home = Path(os.environ.get("XDG_DATA_HOME")
                     or Path.home() / ".local/share")
    cands = [
        here.parent / "data" / XML_FILE,
        data_home / "dbus-1" / "interfaces" / XML_FILE,
        Path("/usr/local/share/dbus-1/interfaces") / XML_FILE,
        Path("/usr/share/dbus-1/interfaces") / XML_FILE,
    ]
    for path in cands:
        try:
            return path.read_text("utf-8")
        except OSError:
            continue
    raise FileNotFoundError("arayuz XML'i bulunamadi: %s" % XML_FILE)


if Gio is not None:
    class _Service(Gio.Application):
        """Adi org.ferdim.Daemon olan servis; nesne /org/ferdim/Daemon."""

        def __init__(self, bridge: Bridge, xml: str, socket_path: str):
            super().__init__(application_id=BUS_NAME,
                             flags=Gio.ApplicationFlags.IS_SERVICE)
            self._bridge = bridge
            self._xml = xml
            self._socket_path = socket_path
            self._conn = None

        def do_dbus_register(self, connection, object_path):
            info = Gio.DBusNodeInfo.new_for_xml(self._xml)
            connection.register_object(OBJECT_PATH, info.interfaces[0],
                                       self._call, self._get_property, None)
            self._conn = connection
            threading.Thread(target=self._watch, name="ferdim-dbus-watch",
                             daemon=True).start()
            return True

        # ------------------------------------------------------------ methods
        def _call(self, connection, sender, path, iface, method, params,
                  invocation):
            try:
                args = params.unpack() if params is not None else ()
                result = self._bridge.call(method, *args)
            except Exception as e:      # BridgeError / baglanti hatasi
                invocation.return_dbus_error(INTERFACE + ".Error", str(e))
                return
            if result is None:
                invocation.return_value(None)
            else:
                invocation.return_value(GLib.Variant("(s)", (result,)))

        def _get_property(self, connection, sender, path, iface, name):
            if name == "Version":
                return GLib.Variant("s", __version__)
            return None

        # ------------------------------------------------------------ signal
        def _watch(self):
            """Daemon'un 'watch' akisini Changed(json) sinyaline cevirir."""
            while True:
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(10.0)
                    sock.connect(self._socket_path)
                    sock.sendall(b'{"cmd":"watch"}\n')
                    stream = sock.makefile("rb")
                    for line in stream:
                        text = line.decode("utf-8", "replace").strip()
                        if text:
                            GLib.idle_add(self._emit, text)
                    sock.close()
                except Exception:
                    pass
                time.sleep(2.0)        # daemon kapali: sessizce yeniden dene

        def _emit(self, text: str):
            if self._conn is not None:
                try:
                    self._conn.emit_signal(
                        None, OBJECT_PATH, INTERFACE, "Changed",
                        GLib.Variant("(s)", (text,)))
                except Exception:
                    pass
            return False               # GLib.idle_add: tek seferlik


def main(argv=None) -> int:
    if Gio is None:
        print("ferdim-dbus: python3-gobject (gi/Gio) gerekli",
              file=sys.stderr)
        return 2
    try:
        xml = introspection_xml()
    except OSError as e:
        print("ferdim-dbus: %s" % e, file=sys.stderr)
        return 1
    env = AppEnv()
    bridge = Bridge(FerdimClient(str(env.socket_path)))
    app = _Service(bridge, xml, str(env.socket_path))
    return app.run([sys.argv[0], *(argv or [])])


if __name__ == "__main__":
    raise SystemExit(main())
