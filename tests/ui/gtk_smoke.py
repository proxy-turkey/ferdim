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
    def windows(cls):
        return [w for w in Gtk.Window.list_toplevels()
                if isinstance(w, cls)]

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
            GLib.timeout_add(300, self.submit_add, False)
            GLib.timeout_add(900, self.submit_bad)
            GLib.timeout_add(1800, self.open_settings)
            GLib.timeout_add(2400, self.settings_flows)
            GLib.timeout_add(3000, self.confirm)
        except Exception as e:
            self.actions.append(("exception", repr(e)))
        return False

    def submit_add(self, bad):
        try:
            wins = self.windows(A.AddWindow)
            if not wins:
                self.actions.append(("exception", "ekleme penceresi yok"))
                return False
            w = wins[-1]
            w.url.set_text("http://127.0.0.1:1/dosya.bin")
            if bad:
                w.chk.set_text("zzz")
            w.submit()
            self.actions.append(("add-window", "bad" if bad else "ok"))
            if bad:
                w.close()
        except Exception as e:
            self.actions.append(("exception", repr(e)))
        return False

    def submit_bad(self):
        self.on_add(None)
        GLib.timeout_add(300, self.submit_add, True)
        return False

    def cfg(self):
        try:
            return self.client.request("config-get").get("config") or {}
        except Exception:
            return {}

    def open_settings(self):
        if self.client:      # taban: turbo acik (varsayilan azami guc)
            try:
                self.client.request("turbo", on=True)
            except Exception:
                pass
        self.on_settings(None)
        return False

    def settings_flows(self):
        try:
            wins = self.windows(A.SettingsWindow)
            self.actions.append(("settings-window", len(wins) == 1))
            if not wins:
                return False
            w = wins[-1]
            base = {k: self.cfg().get(k) for k in
                    ("turbo_mode", "speed_limit", "max_concurrent",
                     "turbo_prev")}
            w.save()                     # hicbir alana dokunulmadi
            after = self.cfg()
            self.actions.append(("settings-untouched", all(
                after.get(k) == base[k] for k in base)))
            self.actions.append(("settings-saved-label",
                                 w.status.get_text() == A.t("saved", A._LANG)))
            # turbo acikken secilen limit yok sayilir (celiski olmaz)
            w.limit.set_selected(3)      # 1 MB; alan etkisiz olsa da secilir
            w.save()
            now = self.cfg()
            self.actions.append(("settings-turbo-limit-guard",
                                 bool(now.get("turbo_mode"))
                                 and int(now.get("speed_limit") or 0) == 0
                                 and w.limit.get_selected() == 0))
            # limit secmek turbo'yu kapatir
            w.turbo.set_active(False)
            w.limit.set_selected(3)      # 1 MB
            w.conc.set_value(6)
            w.save()
            now = self.cfg()
            self.actions.append(("settings-limit-turbo-off",
                                 not now.get("turbo_mode")
                                 and int(now.get("speed_limit") or 0)
                                 == A.SPEED_PRESETS[3]
                                 and int(now.get("max_concurrent") or 0) == 6))
            # stall_timeout timeout'a kirpilir ve ekranda gorunur
            w.timeout.set_value(15)
            w.stall.set_value(120)
            w.save()
            now = self.cfg()
            self.actions.append(("settings-stall-clamp",
                                 int(now.get("stall_timeout") or 0) == 15
                                 and int(w.stall.get_value()) == 15))
            # turbo ac/kapa onceki limiti geri getirir; Save limit komutu
            # gondermez (ekranda gorunen = kaydedilen)
            w.turbo.set_active(True)     # 1 MB saklanir, alan limitsize doner
            w.turbo.set_active(False)    # 1 MB geri gelir
            sent = []
            orig = self.client.request

            def spy(cmd, **kw):
                sent.append(cmd)
                return orig(cmd, **kw)
            self.client.request = spy
            try:
                w.save()
            finally:
                self.client.request = orig
            now = self.cfg()
            self.actions.append(("settings-turbo-restore",
                                 int(now.get("speed_limit") or 0)
                                 == A.SPEED_PRESETS[3]
                                 and w.limit.get_selected() == 3
                                 and "limit" not in sent
                                 and "turbo" not in sent))
            # turbo acikken acilan pencere daemon'in geri yukleyecegi
            # limiti gosterir (turbo_prev'teki 1 MB)
            self.client.request("turbo", on=True)
            w.close()
            GLib.timeout_add(400, self.settings_reopen)
        except Exception as e:
            self.actions.append(("exception", repr(e)))
        return False

    def settings_reopen(self):
        self.on_settings(None)
        GLib.timeout_add(300, self.settings_reopen_check)
        return False

    def settings_reopen_check(self):
        try:
            wins = self.windows(A.SettingsWindow)
            w = wins[-1] if wins else None
            ok = bool(w) and w.turbo.get_active() \
                and w.limit.get_selected() == 0
            if w is not None:
                w.turbo.set_active(False)   # turbo_prev'teki 1 MB gorunur
                ok = ok and w.limit.get_selected() == 3
                w.close()
            self.actions.append(("settings-turbo-reopen", ok))
        except Exception as e:
            self.actions.append(("exception", repr(e)))
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
        leftovers = [type(w).__name__ for w in Gtk.Window.list_toplevels()
                     if isinstance(w, (A.AddWindow, A.SettingsWindow))]
        self.actions.append(("windows-closed", leftovers == []))
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
        for item in (("add-window", "ok"), ("add-window", "bad"),
                     ("settings-window", True),
                     ("settings-untouched", True),
                     ("settings-saved-label", True),
                     ("settings-turbo-limit-guard", True),
                     ("settings-limit-turbo-off", True),
                     ("settings-stall-clamp", True),
                     ("settings-turbo-restore", True),
                     ("settings-turbo-reopen", True),
                     ("windows-closed", True)):
            if item not in self.actions:
                print("   EKSIK:", item)
                ok = False
        print("GTK SMOKE:", "OK" if ok else "FAIL")
        sys.stdout.flush()
        self.quit()
        return False


app = Smoke()
sys.exit(app.run([]))
