"""GTK duman testi (xvfb altinda, bagimsiz kokinlu daemon ile; bkz. run.sh).
Pencere acilir, satir tum durumlarda guncellenir, ekleme kapanisi dogru ve
hatali girisle gonderilir, onay akisi denetlenir; GLib uyari/kritik mesaji
ve beklenmeyen kural disi durum yok. XDG_* cevre degiskenleri run.sh verir."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib
from ferdim.gui import app as A

LOG = []


def _log(_d, _lvl, msg, _u):
    LOG.append(msg)


for _dom in ("Gtk", "GLib-GObject", "GLib", "Pango", "Gdk"):
    GLib.log_set_handler(_dom, GLib.LogLevelFlags.LEVEL_WARNING, _log, None)

JOB = {"id": "smoke1", "filename": "ornek.zip", "url": "http://h/ornek.zip",
       "state": "downloading", "size": 1000, "done": 420, "speed": 123456,
       "eta": 3.2, "category": "compressed", "fullpath": "/tmp/ornek.zip",
       "segments_cfg": 32, "adaptive": 8, "verified": ["sha256"],
       "checksum": "ok", "mirrors": 2, "sources_dead": 1,
       "window": "02:00-08:00", "sched_wait": True, "verifying": False,
       "created": 0, "kind": "http", "dir": "", "segments": 4,
       "segments_effective": 8, "best_speed": 0, "error": ""}


def walk(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from walk(child)
        child = child.get_next_sibling()


class Smoke(A.FerdimApp):
    def __init__(self):
        super().__init__()
        self.actions = []
        self.row = None

    def _error(self, msg, parent=None):
        self.actions.append(("error", msg))
        Gtk.AlertDialog(message=msg).show(parent or self.get_active_window())

    def do_activate(self):
        super().do_activate()
        GLib.timeout_add(900, self.step)
        GLib.timeout_add(7000, self.finish)

    @staticmethod
    def dialogs():
        return [w for w in Gtk.Window.list_toplevels()
                if isinstance(w, Gtk.Dialog)]

    def step(self):
        try:
            self.row = A.JobRow(self, dict(JOB))
            self.listbox.append(self.row)
            for st in A.STATES:
                self.row.update({**JOB, "state": st,
                                 "verifying": st == "downloading",
                                 "error": "ornek hata" if st == "error" else ""})
            self.actions.append(("done-buttons", sorted(
                a for a, b in self.row.btn.items() if b.get_visible())))
            self.action("yok", "pause")
            self.on_add(None)
            GLib.timeout_add(400, self.submit, False)
            GLib.timeout_add(1500, self.submit_bad)
            GLib.timeout_add(2700, self.confirm)
        except Exception as e:
            self.actions.append(("exception", repr(e)))
        return False

    def submit(self, bad):
        try:
            dlg = self.dialogs()[-1]
            entries = [w for w in walk(dlg.get_content_area())
                       if isinstance(w, Gtk.Entry)]
            entries[0].set_text("http://127.0.0.1:1/dosya.bin")
            if bad:
                entries[-1].set_text("zzz")
            dlg.response(Gtk.ResponseType.OK)
        except Exception as e:
            self.actions.append(("exception", repr(e)))
        return False

    def submit_bad(self):
        self.on_add(None)
        GLib.timeout_add(400, self.submit, True)
        return False

    def confirm(self):
        self._confirm_remove("yok")
        GLib.timeout_add(400, self.confirm_check)
        return False

    def confirm_check(self):
        # onay kapanisi cevabi: yalniz 'Sil' (1) secenagi silmeyi tetikler
        done = []
        self._do_action = lambda jid, act: done.append((jid, act))
        self._confirm_remove("is1")            # kapanis hatasiz acilir
        calls = []

        class Fake:
            def __init__(self, answer):
                self.answer = answer

            def choose_finish(self, res):
                calls.append(res)
                return self.answer
        self._confirm_done(Fake(0), None, "is1")
        self._confirm_done(Fake(1), None, "is1")
        self.actions.append(("confirm-flow", (done == [("is1", "remove")], len(calls) == 2)))
        return False

    def finish(self):
        ok = True
        for kind, val in self.actions:
            print("  ", kind, ":", val)
        print("   durum:", self.status.get_text())
        for m in LOG[:10]:
            print("   LOG:", m.strip()[:200])
        loud = [m for m in LOG if "CRITICAL" in m or "Warning" in m]
        if loud or any(k == "exception" for k, _ in self.actions):
            ok = False
        if ("error", A.t("cli_bad_checksum", A._LANG)) not in self.actions:
            print("   EKSIK: gecersiz saglama hatasi gosterilmedi")
            ok = False
        if ("error", A.t("err_url", A._LANG)) in self.actions:
            ok = False
        print("GTK SMOKE:", "OK" if ok else "FAIL")
        sys.stdout.flush()
        self.quit()
        return False


app = Smoke()
sys.exit(app.run([]))
