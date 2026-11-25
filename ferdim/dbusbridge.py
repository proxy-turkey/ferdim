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

# 10 000 islik kuyrukta tam istatistik govdesi ~6-7 MB: ne D-Bus yontemi
# ne de Changed sinyali bunu tasimali. Iki sinir da burada:
#  - Stats: D-Bus imzasi DEGISMEZ (arguman yok; eski cagiranlar calisir),
#    varsayilan pencere uygulanir. Toplamlar (total_speed/active/
#    total_jobs) sunucuda TUM kuyruk uzerinden hesaplandigi icin yalniz
#    toplam isteyen istemci bir sey kaybetmez.
#  - Changed: her karede yalniz toplamlar + etkin isler gider (biten/
#    iptal isler ve govdenin geri kalani kirpilir).
STATS_JOBS_DEFAULT = 200
CHANGED_JOBS_MAX = 50
_DONE_STATES = ("done", "canceled")
_CHANGED_FIELDS = ("total_speed", "active", "total_jobs", "max_concurrent",
                   "speed_limit", "turbo", "nm_connected", "warning",
                   "version")


def stats_params(limit=None) -> dict:
    """`stats` komut parametreleri: pencere varsayilanla sinirli.

    Gecersiz/sifir limit varsayilana doner; sunucu da kendi ust sinirini
    uygular (`STATS_LIMIT_MAX`)."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = STATS_JOBS_DEFAULT
    if n <= 0:
        n = STATS_JOBS_DEFAULT
    return {"limit": n}


def compact_frame(text: str) -> str:
    """watch karesini Changed icin kucultur: toplamlar + etkin isler.

    Panel toplam hizi/etkin is sayisini, menu de duraklatilabilir isleri
    gosterir: 10 000 islik kuyrukta tam kare (~6-7 MB) her karede D-Bus
    sinyalinden ve kabuk JS'inden gecmemeli. Bozuk/JSON olmayan satir bos
    dize doner (sinyal uretilmez). Biten/iptal isler listeye girmez;
    `jobs_total` kirpilmadan onceki etkin is sayisidir."""
    try:
        frame = json.loads(text)
    except (TypeError, ValueError):
        return ""
    if not isinstance(frame, dict):
        return ""
    jobs = [j for j in (frame.get("jobs") or ())
            if isinstance(j, dict) and j.get("state") not in _DONE_STATES]
    out = {key: frame[key] for key in _CHANGED_FIELDS if key in frame}
    out["jobs"] = jobs[:CHANGED_JOBS_MAX]
    out["jobs_total"] = len(jobs)
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


# Dosya kurulu olmasa da kopru calissin: data/ ile ayni arayuz.
_FALLBACK_XML = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.ferdim.Daemon1">
    <method name="Add">
      <arg type="s" name="url" direction="in"/>
      <arg type="s" name="id" direction="out"/>
    </method>
    <method name="Pause">
      <arg type="s" name="id" direction="in"/>
    </method>
    <method name="Resume">
      <arg type="s" name="id" direction="in"/>
    </method>
    <method name="Cancel">
      <arg type="s" name="id" direction="in"/>
    </method>
    <method name="List">
      <arg type="s" name="json" direction="out"/>
    </method>
    <method name="Stats">
      <arg type="s" name="json" direction="out"/>
    </method>
    <method name="WebUrl">
      <arg type="s" name="url" direction="out"/>
    </method>
    <property name="Version" type="s" access="read"/>
    <signal name="Changed">
      <arg type="s" name="json"/>
    </signal>
  </interface>
</node>
"""


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
        # D-Bus'ta arguman yok (imza degismez); pencere koprude sinirlanir.
        return "stats", stats_params(args[0] if args else None)
    if method == "WebUrl":
        return "web-session", {}
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
            raise BridgeError(str(resp.get("error") or resp.get("code")
                                  or "ferdim hatasi"))
        if method == "Add":
            return str(resp.get("id") or "")
        if method in ("List", "Stats"):
            return json.dumps(resp, ensure_ascii=False)
        if method == "WebUrl":
            url = str(resp.get("url") or "")
            if not url:
                raise BridgeError("web-session: URL donmedi")
            return url
        return None


def introspection_xml(candidates=None) -> str:
    """Arayuz XML'i: depo 'data/' dizini ya da kurulu standart yollar.

    Hicbiri yoksa gomulu kopya doner; boylece eksik veri dosyasi
    yuzunden kopru acilista olmez."""
    here = Path(__file__).resolve().parent
    data_home = Path(os.environ.get("XDG_DATA_HOME")
                     or Path.home() / ".local/share")
    cands = [
        here.parent / "data" / XML_FILE,
        data_home / "dbus-1" / "interfaces" / XML_FILE,
        Path("/usr/local/share/dbus-1/interfaces") / XML_FILE,
        Path("/usr/share/dbus-1/interfaces") / XML_FILE,
    ]
    for path in (cands if candidates is None else candidates):
        try:
            return path.read_text("utf-8")
        except OSError:
            continue
    return _FALLBACK_XML


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
            """Daemon'un 'watch' akisini Changed(json) sinyaline cevirir.

            Kare burada KUCULTULUR (`compact_frame`): kaynak tam kareyi
            10 000 ise kadar tasiyabilir, kabuga yalniz toplamlar + etkin
            isler gitmeli."""
            while True:
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(10.0)
                    sock.connect(self._socket_path)
                    sock.sendall(b'{"cmd":"watch"}\n')
                    stream = sock.makefile("rb")
                    for line in stream:
                        text = line.decode("utf-8", "replace").strip()
                        if not text:
                            continue
                        compact = compact_frame(text)
                        if compact:
                            GLib.idle_add(self._emit, compact)
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
