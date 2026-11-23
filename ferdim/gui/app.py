"""FerDim GTK4 arayuzu (python3-gobject).

PyGObject yoksa net bir Turkce hata verir; arayuzsuz kullanim
(ferdim CLI / web) etkilenmez. Simgeli dugmelere erisilebilir ad, giris
alanlarina animsatici (mnemonic) etiket eklenir; boylece ekran okuyucular
alani adiyla duyurur. GTK 4.10 oncesinde bulunmayan erisilebilirlik
cagrilari sessizce atlanir.
"""
from __future__ import annotations

import sys

try:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk
except (ImportError, ValueError) as e:  # pragma: no cover
    print("HATA: GTK4 arayuzu icin 'python3-gobject' paketi gerekli.")
    print("  sudo dnf install python3-gobject")
    print("Ayrinti:", e)
    sys.exit(1)

from .. import __version__, util
from ..client import FerdimClient, ensure_daemon
from ..i18n import detect_lang, t
from ..paths import AppEnv

STATES = ("queued", "connecting", "downloading", "paused",
          "done", "error", "canceled")
# (eylem, i18n anahtari, simge)
ACTIONS = (("resume", "act_resume", "media-playback-start"),
           ("pause", "act_pause", "media-playback-pause"),
           ("restart", "act_restart", "view-refresh"),
           ("remove", "act_remove", "user-trash"))

_LANG = "tr"


def set_gui_lang(cfg_lang: str | None) -> None:
    global _LANG
    _LANG = detect_lang(cfg_lang)


def tr_state(state: str) -> str:
    return t("st_" + state, _LANG) if state in STATES else state


def _a11y(widget, name: str, values: list) -> None:
    """Erisilebilirlik ozelligi (Gtk.AccessibleProperty): yalniz simgeli
    dugmenin adi, giris alaninin ipucu. GTK surumunde yoksa atlanir."""
    try:
        widget.update_property([getattr(Gtk.AccessibleProperty, name)], values)
    except (AttributeError, TypeError):
        pass


def row_notes(job: dict) -> list[str]:
    """Satirin ikinci satirindaki kisa notlar (sunucu limiti, saglama,
    kaynak, zamanlama penceresi)."""
    out = []
    if job.get("adaptive") and job.get("adaptive") < job.get("segments_cfg", 0):
        out.append("⚙ " + t("adaptive_limit", _LANG).replace(
            "{n}", str(job["adaptive"])))
    if job.get("verifying"):
        out.append("⏳ " + t("st_verifying", _LANG))
    if job.get("error"):
        out.append("⚠ " + job["error"])
    if job.get("sched_wait") and job.get("window"):
        out.append("⏱ " + t("sched_wait", _LANG).replace(
            "{w}", job["window"]))
    if job.get("sources_dead"):
        out.append("⛔ " + t("mirrors_dead", _LANG).replace(
            "{n}", str(job["sources_dead"])))
    if job.get("state") == "done" and job.get("checksum") == "warn":
        out.append("⚠ " + t("chk_warn", _LANG))
    verified = job.get("verified") or []
    if job.get("state") == "done" and verified:
        out.append("✓ " + t("chk_ok", _LANG).replace("{a}", ", ".join(verified)))
    return out


class JobRow(Gtk.ListBoxRow):
    def __init__(self, app, job):
        super().__init__()
        self.app = app
        self.job_id = job["id"]
        self.btn: dict[str, Gtk.Button] = {}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6); box.set_margin_bottom(6)
        box.set_margin_start(8); box.set_margin_end(8)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.name = Gtk.Label(label=job["filename"], hexpand=True, xalign=0,
                              ellipsize=3)
        self.name.get_style_context().add_class("heading")
        self.state = Gtk.Label()
        self.state.get_style_context().add_class("dim-label")
        head.append(self.name); head.append(self.state)
        box.append(head)

        self.progress = Gtk.ProgressBar(hexpand=True, pulse_step=0.08)
        self.progress.set_show_text(True)
        box.append(self.progress)

        info = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.info = Gtk.Label(label="", xalign=0, hexpand=True, ellipsize=2,
                              selectable=True)
        self.info.get_style_context().add_class("dim-label")
        info.append(self.info)

        for action, key, icon in ACTIONS:
            tip = t(key, _LANG)
            b = Gtk.Button(icon_name=icon, tooltip_text=tip,
                           valign=Gtk.Align.CENTER)
            b.connect("clicked", self._act, action)
            b.get_style_context().add_class("flat")
            _a11y(b, "LABEL", [tip])                 # simgeli dugmenin adi
            self.btn[action] = b
            info.append(b)
        box.append(info)
        self.set_child(box)
        self.update(job)

    def _act(self, _btn, action):
        self.app.action(self.job_id, action)

    def update(self, job):
        st = job["state"]
        self.state.set_label(tr_state(st))
        if job["size"] > 0:
            frac = min(1.0, job["done"] / job["size"])
            self.progress.set_fraction(frac)
            self.progress.set_text(f"{int(frac * 100)}%")
        else:
            self.progress.pulse()
            self.progress.set_text(util.human_size(job["done"]))
        playing = st in ("connecting", "downloading")
        self.btn["resume"].set_visible(
            st in ("paused", "error", "queued", "canceled"))
        self.btn["pause"].set_visible(playing)
        parts = [f"{util.human_size(job['done'])} / "
                 f"{util.human_size(job['size'])}"]
        if playing:
            parts.append(f"{util.human_size(job['speed'])}{t('per_sec', _LANG)}"
                         f"   {t('col_eta', _LANG)}: {util.human_eta(job['eta'])}")
        parts.extend(row_notes(job))
        parts.append(job["fullpath"])
        self.info.set_text("   ".join(parts))


class FerdimApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="app.ferdim.gui")
        self.rows: dict[str, JobRow] = {}
        self.client: FerdimClient | None = None
        self.env = AppEnv()
        self.failures = 0

    # ------------------------------------------------------------- lifecycle
    def do_activate(self):
        set_gui_lang(self.env.load_config().get("language"))
        win = Gtk.ApplicationWindow(application=self)
        win.set_title(f"FerDim {__version__} — {t('app_title', _LANG)}")
        win.set_default_size(780, 480)

        hb = Gtk.HeaderBar()
        win.set_titlebar(hb)

        add_btn = Gtk.Button(label=t("h_new", _LANG))
        add_btn.get_style_context().add_class("suggested-action")
        add_btn.connect("clicked", self.on_add)
        hb.pack_start(add_btn)

        self.turbo_btn = Gtk.Button(label="⚡ " + t("turbo", _LANG),
                                    tooltip_text=t("turbo_hint", _LANG))
        self.turbo_btn.connect("clicked", self.on_turbo)
        hb.pack_start(self.turbo_btn)

        web_btn = Gtk.Button(label=t("web_ui", _LANG))
        web_btn.connect("clicked", self.on_web)
        hb.pack_end(web_btn)

        self.status = Gtk.Label(label=t("daemon_ok", _LANG), xalign=0,
                                ellipsize=2)
        self.status.get_style_context().add_class("dim-label")
        self.status.set_margin_start(8); self.status.set_margin_end(8)

        self.listbox = Gtk.ListBox(valign=Gtk.Align.START,
                                   selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.set_placeholder(Gtk.Label(label=t("empty_title", _LANG)))
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_child(self.listbox)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6); box.set_margin_bottom(6)
        box.append(sw)
        box.append(self.status)
        win.set_child(box)
        win.present()

        if ensure_daemon(self.env):
            self.connect_client()
            GLib.timeout_add(500, self.refresh)
            self.refresh()
        else:
            self.status.set_text(t("daemon_down", _LANG))

    def connect_client(self):
        try:
            self.client = FerdimClient(str(self.env.socket_path)).connect()
            return True
        except OSError:
            self.client = FerdimClient(str(self.env.socket_path))
            return False

    # ------------------------------------------------------------- handlers
    def refresh(self):
        if not self.client:
            return True
        try:
            st = self.client.request("stats")
        except Exception:
            # baglanti kopmus olabilir: sonraki taramada yeniden baglanilir;
            # daemon yoksa (kapanmissa) bir kez yeniden baslatilmaya calisilir
            self.client.close()
            self.failures += 1
            if self.failures == 2:
                ensure_daemon(self.env)
            self.status.set_text(t("daemon_lost", _LANG))
            return True
        self.failures = 0
        jobs = st["jobs"]
        seen = set()
        for j in jobs:
            seen.add(j["id"])
            row = self.rows.get(j["id"])
            if row is None:
                row = JobRow(self, j)
                self.rows[j["id"]] = row
                self.listbox.append(row)
            else:
                row.update(j)
        for jid in list(self.rows):
            if jid not in seen:
                self.listbox.remove(self.rows[jid])
                del self.rows[jid]
        nm = t("nm_ok", _LANG) if st.get("nm_connected") else t("nm_off", _LANG)
        self.status.set_text(
            f"{t('total_speed', _LANG)}: "
            f"{util.human_size(st['total_speed'])}{t('per_sec', _LANG)}   "
            f"{t('active_jobs', _LANG)}: {st['active']}/{st['max_concurrent']}   "
            f"{t('ext_state', _LANG)}: {nm}")
        turbo = bool(st.get("turbo"))
        self.turbo_btn.set_label(
            "⚡ " + (t("turbo_on", _LANG) if turbo else t("turbo", _LANG)))
        ctx = self.turbo_btn.get_style_context()
        if turbo:
            ctx.add_class("destructive-action")
        else:
            ctx.remove_class("destructive-action")
        return True

    def on_turbo(self, _btn):
        if not self.client:
            return
        try:
            self.client.request("turbo", on=not bool(
                self.client.request("stats").get("turbo")))
        except Exception:
            pass
        self.refresh()

    def action(self, job_id, action):
        if action == "remove":
            self._confirm_remove(job_id)
        else:
            self._do_action(job_id, action)

    def _do_action(self, job_id, action):
        if not self.client:
            return
        try:
            if action == "remove":
                self.client.request("remove", id=job_id, delete_files=True)
            else:
                self.client.request(action, id=job_id)
        except Exception:
            pass
        self.refresh()

    def _confirm_remove(self, job_id):
        """Silme onayi; GTK < 4.10'da onay penceresi yok, dogrudan silinir."""
        win = self.get_active_window()
        if not hasattr(Gtk, "AlertDialog"):
            self._do_action(job_id, "remove")
            return
        ad = Gtk.AlertDialog(message=t("confirm_remove", _LANG),
                             buttons=[t("btn_cancel", _LANG),
                                      t("act_remove", _LANG)])
        ad.set_cancel_button(0)
        ad.set_default_button(1)
        ad.choose(win, None, lambda d, res: self._confirm_done(d, res, job_id))

    def _confirm_done(self, dialog, res, job_id):
        try:
            if dialog.choose_finish(res) == 1:
                self._do_action(job_id, "remove")
        except Exception:
            pass

    def _error(self, msg, parent=None):
        win = parent or self.get_active_window()
        if hasattr(Gtk, "AlertDialog"):              # GTK >= 4.10
            Gtk.AlertDialog(message=msg).show(win)
            return
        md = Gtk.MessageDialog(transient_for=win, modal=True,
                               message_type=Gtk.MessageType.ERROR,
                               buttons=Gtk.ButtonsType.OK, text=msg)
        md.connect("response", lambda d, _r: d.destroy())
        md.present()

    def on_add(self, _btn):
        dlg = Gtk.Dialog(transient_for=self.get_active_window(),
                         modal=True, title=t("h_new", _LANG))
        dlg.add_button(t("btn_cancel", _LANG), Gtk.ResponseType.CANCEL)
        dlg.add_button(t("btn_download", _LANG), Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        area = dlg.get_content_area()
        area.set_spacing(4)
        area.set_margin_top(10); area.set_margin_bottom(10)
        area.set_margin_start(14); area.set_margin_end(14)

        def field(widget, key, target=None):
            # animsatici etiket: Alt+harf alana gecer, GTK etiket <-> alan
            # iliskisini (LABELLED_BY / LABEL_FOR) ekran okuyuculara verir
            lab = Gtk.Label(xalign=0, use_underline=True,
                            mnemonic_widget=target or widget)
            lab.set_label("_" + t(key, _LANG).replace("_", "__"))
            area.append(lab)
            area.append(widget)

        entry = Gtk.Entry(placeholder_text="https://...", hexpand=True,
                          activates_default=True)
        field(entry, "lbl_url")

        view = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.set_left_margin(6); view.set_right_margin(6)
        sw = Gtk.ScrolledWindow(height_request=52, hexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER,
                                vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        sw.set_child(view)
        _a11y(view, "PLACEHOLDER", ["https://mirror.example.org/dosya.iso"])
        field(sw, "lbl_mirrors", target=view)

        seg = Gtk.SpinButton.new_with_range(1, 32, 1)
        try:    # sunucu varsayilani (turbo'da 32, degilse config'ten)
            seg.set_value(int(self.client.request("ping").get("segments") or 8))
        except Exception:
            seg.set_value(8)
        field(seg, "lbl_segments")

        chk = Gtk.Entry(placeholder_text=t("ph_checksum", _LANG), hexpand=True)
        field(chk, "lbl_checksum")

        now = Gtk.CheckButton(use_underline=True, active=True)
        now.set_label("_" + t("start_now", _LANG))
        area.append(now)

        def on_response(d, resp):
            if resp != Gtk.ResponseType.OK:
                d.destroy()
                return
            url = entry.get_text().strip()
            if not url.startswith(("http://", "https://")):
                self._error(t("err_url", _LANG), d)
                return
            digest = chk.get_text().strip()
            if digest and not util.parse_checksum_spec(digest)[0]:
                self._error(t("cli_bad_checksum", _LANG), d)
                return
            buf = view.get_buffer()
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
            mirrors = [u for u in text.split()
                       if u.startswith(("http://", "https://"))][:24]
            segments = int(seg.get_value())
            paused = not now.get_active()
            d.destroy()
            if self.client:
                try:
                    r = self.client.request("add", url=url, segments=segments,
                                            checksum=digest, mirrors=mirrors,
                                            paused=paused)
                except Exception as e:
                    r = {"ok": False, "error": str(e)}
                if not r.get("ok"):
                    self._error(t("add_failed", _LANG) + " " + str(r.get("error", "")))
            self.refresh()

        dlg.connect("response", on_response)
        dlg.present()

    def on_web(self, _btn):
        import subprocess
        ensure_daemon(self.env)
        try:
            port = self.env.port_path.read_text().strip()
            subprocess.Popen(["xdg-open", f"http://127.0.0.1:{port}"])
        except Exception:
            pass


def main() -> int:
    app = FerdimApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
