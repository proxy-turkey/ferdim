"""GTK duman testi (xvfb altinda, bagimsiz kokinlu daemon ile; bkz. run.sh).

Pencere acilir, satir tum durumlarda guncellenir, ekleme kapanisi dogru ve
hatali girisle gonderilir, onay akisi denetlenir; ayrica 1.7 pencereleri
(arsiv, gecmis, ana makine profilleri), birlikte yasam anahtari ve saglama
rozeti gercek daemon uzerinden denenir. Kucuk bir ZIP yerel HTTP
sunucusundan indirilir; cikarma/gecmis/profil akislari bu is uzerinde
kosar. GLib uyari/kritik mesaji ve beklenmeyen kural disi durum olmamali.
XDG_* cevre degiskenleri run.sh verir."""
import http.server
import os
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib
from ferdim.gui import app as A
from ferdim.gui.archive_win import ArchiveWindow
from ferdim.gui.history_win import HistoryWindow
from ferdim.gui.hosts_win import HostsWindow

LOG = []


def _log(_d, _lvl, msg, _u):
    LOG.append(msg)


for _dom in ("Gtk", "GLib-GObject", "GLib", "Pango", "Gdk"):
    GLib.log_set_handler(_dom, GLib.LogLevelFlags.LEVEL_WARNING, _log, None)

# --------------------------------------------------------------- yerel sunucu
# Kosuya ozgu adlar: ayni XDG kokunu paylasan web testi (run.sh all) ayni
# dosya adlarini indirdigi icin sabit adlar hedef dosya/cikarma dizini ve
# gecmis aramasi uzerinden yarisa girerdi.
WWW = tempfile.mkdtemp(prefix="ferdim-gtk-www-")
RUN = f"gtk-{os.getpid()}-{int(time.time())}"
ZIP_NAME = f"{RUN}-arsiv.zip"
ADD_NAME = f"{RUN}-belge.bin"
with zipfile.ZipFile(os.path.join(WWW, ZIP_NAME), "w") as _z:
    _z.writestr("a.txt", "a" * 3000)
    _z.writestr("b.txt", "b" * 2000)
with open(os.path.join(WWW, ADD_NAME), "wb") as _f:
    _f.write(b"x" * 5000)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_a):
        pass


SRV = http.server.ThreadingHTTPServer(
    ("127.0.0.1", 0), lambda *a, **k: _Quiet(*a, directory=WWW, **k))
threading.Thread(target=SRV.serve_forever, daemon=True).start()
BASE_URL = f"http://127.0.0.1:{SRV.server_address[1]}"
ZIP_URL = BASE_URL + "/" + ZIP_NAME
ADD_URL = BASE_URL + "/" + ADD_NAME

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
        self.zip_job = ""
        self._finished = False

    def _error(self, msg, parent=None):
        self.actions.append(("error", msg))
        Gtk.AlertDialog(message=msg).show(parent or self.get_active_window())

    POLL_MS = 100                 # yoklama araligi (GLib ana dongusu)
    DEADLINE_S = 20.0             # kosul basina comert son tarih

    def until(self, name, cond, then, deadline=None, detail=None):
        """Kosul saglanana kadar GLib ana dongusunde yoklar.

        Sure duvar saatinden olculur (yuk altinda 150 ms'lik sayim
        gercekten 20 sn surmez); son tarih gecerse zaman asimi kaydedilir
        ve zincir yine de devam eder ki duman testi kilitlenmesin."""
        end = time.monotonic() + (self.DEADLINE_S if deadline is None
                                  else float(deadline))

        def tick():
            try:
                ok = bool(cond())
            except Exception:
                ok = False
            if ok or time.monotonic() >= end:
                if not ok:
                    self.actions.append(("timeout", name))
                    if detail is not None:
                        try:
                            print("   DETAIL", name, detail())
                        except Exception:
                            pass
                try:
                    then()
                except Exception as e:
                    self.actions.append(("exception", repr(e)))
                return False
            return True
        GLib.timeout_add(self.POLL_MS, tick)

    def snapshot(self, job_id):
        try:
            for j in self.client.request("stats").get("jobs") or []:
                if j.get("id") == job_id:
                    return j
        except Exception:
            pass
        return None

    def do_activate(self):
        super().do_activate()
        GLib.timeout_add(900, self.step)
        GLib.timeout_add(70000, self._finish_once)

    @staticmethod
    def windows(cls):
        return [w for w in Gtk.Window.list_toplevels()
                if isinstance(w, cls)]

    def step(self):
        try:
            self.seed_profile()
            self.row = A.JobRow(self, dict(JOB))
            self.listbox.append(self.row)
            for st in A.STATES:
                self.row.update({**JOB, "state": st,
                                 "verifying": st == "downloading",
                                 "error": "ornek hata" if st == "error" else ""})
            self.actions.append(("done-buttons", sorted(
                a for a, b in self.row.btn.items() if b.get_visible())))
            self.badge_flow()
            self.action("yok", "pause")
            self.on_add(None)
            GLib.timeout_add(300, self.submit_add, False)
            GLib.timeout_add(900, self.submit_bad)
            GLib.timeout_add(1800, self.open_settings)
            GLib.timeout_add(2400, self.settings_flows)
            GLib.timeout_add(3000, self.start_confirm)
            GLib.timeout_add(4200, self.start_zip)
        except Exception as e:
            self.actions.append(("exception", repr(e)))
        return False

    def seed_profile(self):
        """Kucuk duman dosyalari profil ogrenme esigini (1 MiB) gecmez:
        hosts penceresini denemek icin depoya bir profil tohumla. Tohum,
        daemon deposu dosyayi okumadan ONCE yazilir (acilista is yok)."""
        try:
            from ferdim.paths import AppEnv
            from ferdim.profiles import open_profiles
            open_profiles(AppEnv()).record("127.0.0.1", links=4,
                                           per_link_bps=500000)
        except Exception as e:
            self.actions.append(("exception", repr(e)))

    def badge_flow(self):
        """Saglama/Merkle rozeti: ok / warn / mismatch ve yok durumu."""
        ok = True
        self.row.update({**JOB, "checksum": "ok", "merkle": "ok"})
        ok = ok and self.row.badge.get_visible() \
            and self.row.badge.get_text() == "✓"
        self.row.update({**JOB, "checksum": "warn", "merkle": ""})
        ok = ok and self.row.badge.get_text() == "⚠"
        self.row.update({**JOB, "checksum": "mismatch", "merkle": ""})
        ok = ok and self.row.badge.get_text() == "✗"
        self.row.update({**JOB, "checksum": "", "merkle": "",
                         "verified": []})
        ok = ok and not self.row.badge.get_visible()
        self.actions.append(("integrity-badge", ok))

    # --------------------------------------------------------- ekleme akisi
    def submit_add(self, bad):
        try:
            wins = self.windows(A.AddWindow)
            if not wins:
                self.actions.append(("exception", "ekleme penceresi yok"))
                return False
            w = wins[-1]
            # yerel dosya: erisilemeyen bir URL kuyrukta takili kalip
            # smart_queue siralamasini geciktiriyor (duman testi beklemesin)
            w.url.set_text(ADD_URL)
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

    # --------------------------------------------------------- ayar akisi
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
                                 w.status.get_text() == A.gtr("saved")))
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

    def start_confirm(self):
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
        self._confirm_done(Fake(0), None, lambda: done.append(("no", "x")))
        self._confirm_done(Fake(1), None, lambda: done.append(("is1", "remove")))
        self.actions.append(("confirm-flow", (done == [("is1", "remove")],
                                              len(calls) == 2)))
        return False

    # ------------------------------------------------------- arsiv akisi
    def start_zip(self):
        """Kucuk ZIP'i kuyruga ekler; bitince arsiv penceresini acar."""
        try:
            r = self.client.request("add", url=ZIP_URL, segments=2,
                                    paused=False)
            self.zip_job = r.get("id", "") if r.get("ok") else ""

        except Exception as e:
            self.actions.append(("exception", repr(e)))
        self.until("zip-download", lambda: (self.snapshot(self.zip_job) or {})
                   .get("state") == "done", self.open_archive)
        return False

    def open_archive(self):
        self.action(self.zip_job, "archive")
        wins = self.windows(ArchiveWindow)
        self.actions.append(("archive-window", len(wins) == 1))
        if not wins:
            return
        w = wins[-1]
        self.until("archive-list", lambda: len(w.checks) >= 2,
                   lambda: self.extract_zip(w))

    def extract_zip(self, w):
        self.actions.append(("archive-entries", len(w.checks)))
        for cb, _key in w.checks[1:]:
            cb.set_active(False)          # yalniz ilk uyeyi cikar
        # pencere archive-status terminal (done/error) gorulunce
        # task_done isaretler; disk yalniz ondan sonra denetlenir
        self.until("archive-extract", lambda: w.task_done,
                   lambda: self.archive_extracted(w))
        w.extract()

    def archive_extracted(self, w):
        # varsayilan cikarma hedefi: arsiv yaninda '<ad>/' (uzanti atilir)
        dest = os.path.join(self.cfg().get("download_dir") or "",
                            ZIP_NAME.rsplit(".zip", 1)[0])
        want = A.gtr("zip_extracted", n=1, dest=dest)
        self.actions.append(("archive-extract",
                             w.status.get_text().startswith(want)))
        # yalniz secilen uye cikarildi mi
        self.actions.append(("archive-files", os.path.isfile(
            os.path.join(dest, "a.txt"))
            and not os.path.isfile(os.path.join(dest, "b.txt"))))
        w.close()
        self.phase_history()

    # ------------------------------------------------------ gecmis akisi
    def phase_history(self):
        self.on_history(None)
        wins = self.windows(HistoryWindow)
        self.actions.append(("history-window", len(wins) == 1))
        if not wins:
            return
        w = wins[-1]
        self.until("history-list", lambda: len(w.recs) >= 1,
                   lambda: self.history_search(w))

    def history_search(self, w):
        self.actions.append(("history-rows", len(w.recs) >= 1))
        # kosuya ozgu ad: paylasilan daemon'un gecmisinde web testinden
        # kalan "arsiv.zip" kayitlari da olabilir
        w.search.set_text(RUN + "-arsiv")
        w.load()
        self.until("history-search",
                   lambda: len(w.recs) == 1 and (RUN + "-arsiv") in
                   str(w.recs[0][1].get("filename") or ""),
                   lambda: self.history_redownload(w))

    def history_redownload(self, w):
        if not w.recs:
            self.actions.append(("exception", "gecmis satiri yok"))
            return
        rec = w.recs[0][1]
        before = len(self.client.request("stats").get("jobs") or [])
        w._redownload(None, str(rec.get("id") or ""))
        self.until("history-redownload",
                   lambda: w.status.get_text() != "",
                   lambda: self.history_done(w, before, rec))

    def history_done(self, w, before, rec):
        after = len(self.client.request("stats").get("jobs") or [])
        want = A.gtr("hist_redownloaded",
                     name=rec.get("filename") or rec.get("url") or "")
        self.actions.append(("history-redownload",
                             after == before + 1
                             and w.status.get_text() == want))
        w.close()
        self.phase_hosts()

    # ---------------------------------------------------- profil akisi
    def phase_hosts(self):
        self.on_hosts(None)
        wins = self.windows(HostsWindow)
        self.actions.append(("hosts-window", len(wins) == 1))
        if not wins:
            return
        w = wins[-1]
        # pencere yuklendi mi (bos liste de gecerli bir durumdur: run.sh all
        # kosusunda web testi daemon'un profil deposunu onceden okumus olur,
        # tohum o zaman gorunmez)
        self.until("hosts-list", lambda: w.loaded,
                   lambda: self.hosts_ready(w),
                   detail=lambda: (w.loaded, len(w.rows),
                                   w.status.get_text()))

    def hosts_ready(self, w):
        """Profil varsa unut akislarini, yoksa bos durumu denetle."""
        if not w.rows:
            self.actions.append(("hosts-state",
                                 w.status.get_text() == A.gtr("hosts_empty")
                                 and not w.forget_all_btn.get_sensitive()))
            w.close()
            self.phase_coexist()
            return
        self.actions.append(("hosts-state", True))
        self.actions.append(("hosts-row", True))
        # satir kendi anahtarini tasir: cocuk zinciri kazilmez (liste
        # yenilenirken cocuk None olabilir)
        self._host = str(getattr(w.rows[0], "host", ""))
        w._forget(None, self._host)
        marked = []

        def forgotten_ok():
            ok = (not w.rows
                  and w.status.get_text()
                  == A.gtr("hosts_forgotten", host=self._host))
            if ok and not marked:
                marked.append(True)
                self.actions.append(("hosts-forget", True))
            return ok

        self.until("hosts-forget", forgotten_ok, lambda: self.hosts_clear(w),
                   detail=lambda: (self._host, w.status.get_text(),
                                   len(self.client.request("hosts")
                                       .get("hosts") or [])))

    def hosts_clear(self, w):
        self.confirm = lambda msg, on_yes, label: on_yes()
        w.forget_all()
        marked = []

        def cleared_ok():
            ok = (not w.forget_all_btn.get_sensitive()
                  and w.status.get_text() != "")
            if ok and not marked:
                marked.append(True)
                self.actions.append(("hosts-forget-all", True))
            return ok

        self.until("hosts-forget-all", cleared_ok,
                   lambda: self.phase_coexist(w))

    def phase_coexist(self, w=None):
        if w is not None:
            w.close()
        self.on_settings(None)
        wins = self.windows(A.SettingsWindow)
        if not wins:
            self.actions.append(("exception", "ayar penceresi yok"))
            return
        sw = wins[-1]
        sw.coexist.set_active(True)
        sw.save()
        st = self.client.request("stats").get("coexist") or {}
        label = self.coexist_lbl.get_text()
        self.actions.append(("coexist-toggle", bool(st.get("enabled"))))
        self.actions.append(("coexist-label",
                             A.gtr("coexist") in label
                             and A.gtr("coexist_on") in label))
        sw.coexist.set_active(False)          # ayari geri birak
        sw.save()
        self.actions.append(("coexist-restore",
                             not (self.client.request("stats")
                                  .get("coexist") or {}).get("enabled")))
        sw.close()
        self._finish_once()

    # ------------------------------------------------------------- bitis
    def _finish_once(self):
        if self._finished:
            return False
        self._finished = True
        self.finish()
        return False

    def finish(self):
        ok = True
        leftovers = [type(w).__name__ for w in Gtk.Window.list_toplevels()
                     if isinstance(w, (A.AddWindow, A.SettingsWindow,
                                       ArchiveWindow, HistoryWindow,
                                       HostsWindow))]
        self.actions.append(("windows-closed", leftovers == []))
        for kind, val in self.actions:
            print("  ", kind, ":", val)
        print("   durum:", self.status.get_text())
        for m in LOG[:10]:
            print("   LOG:", m.strip()[:200])
        loud = [m for m in LOG if "CRITICAL" in m or "Warning" in m]
        if loud or any(k == "exception" for k, _ in self.actions):
            ok = False
        if ("error", A.gtr("cli_bad_checksum")) not in self.actions:
            print("   EKSIK: gecersiz saglama hatasi gosterilmedi")
            ok = False
        if ("error", A.gtr("err_url")) in self.actions:
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
                     ("integrity-badge", True),
                     ("archive-window", True),
                     ("archive-entries", 2),
                     ("archive-extract", True),
                     ("archive-files", True),
                     ("history-window", True),
                     ("history-rows", True),
                     ("history-redownload", True),
                     ("hosts-window", True),
                     ("hosts-state", True),
                     ("coexist-toggle", True),
                     ("coexist-label", True),
                     ("coexist-restore", True),
                     ("windows-closed", True)):
            if item not in self.actions:
                print("   EKSIK:", item)
                ok = False
        print("GTK SMOKE:", "OK" if ok else "FAIL")
        sys.stdout.flush()
        self.quit()


app = Smoke()
sys.exit(app.run([]))
