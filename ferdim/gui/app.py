"""FerDim GTK4 arayuzu (python3-gobject).

PyGObject yoksa net bir Turkish hata verir; arayuzsuz kullanim
(ferdim CLI / web) etkilenmez.
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

_LANG = "tr"


def set_gui_lang(cfg_lang: str | None) -> None:
    global _LANG
    _LANG = detect_lang(cfg_lang)


def tr_state(state: str) -> str:
    return t("st_" + state, _LANG) if state in STATES else state


class JobRow(Gtk.ListBoxRow):
    def __init__(self, app, job):
        super().__init__()
        self.app = app
        self.job_id = job["id"]
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6); box.set_margin_bottom(6)
        box.set_margin_start(8); box.set_margin_end(8)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.name = Gtk.Label(label=job["filename"], hexpand=True, xalign=0,
                              ellipsize=3)
        self.name.get_style_context().add_class("heading")
        self.state = Gtk.Label(label=tr_state(job["state"]))
        head.append(self.name); head.append(self.state)
        box.append(head)

        self.progress = Gtk.ProgressBar(hexpand=True, pulse_step=0.08)
        box.append(self.progress)

        info = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.info = Gtk.Label(label="", xalign=0, hexpand=True)
        self.info.get_style_context().add_class("dim-label")
        info.append(self.info)

        for icon, action, tip in (
                ("media-playback-start", "resume", "Devam"),
                ("media-playback-pause", "pause", "Duraklat"),
                ("view-refresh", "restart", "Baştan indir"),
                ("user-trash", "remove", "Sil")):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
            b.connect("clicked", self._act, action)
            b.get_style_context().add_class("flat")
            info.append(b)
        box.append(info)
        self.set_child(box)
        self.update(job)

    def _act(self, _btn, action):
        self.app.action(self.job_id, action)

    def update(self, job):
        self.state.set_label(tr_state(job["state"]))
        if job["size"] > 0:
            self.progress.set_fraction(min(1.0, job["done"] / job["size"]))
        else:
            self.progress.pulse()
        err = f"  ⚠ {job['error']}" if job.get("error") else ""
        adaptive = ""
        if job.get("adaptive") and job.get("adaptive") < job.get("segments_cfg", 0):
            adaptive = "  ⚙ " + t("adaptive_limit", _LANG).replace(
                "{n}", str(job["adaptive"]))
        self.info.set_text(
            f"{util.human_size(job['done'])} / {util.human_size(job['size'])}   "
            f"{util.human_size(job['speed'])}/s   {t('col_eta', _LANG)}: {util.human_eta(job['eta'])}"
            f"{adaptive}   {job['fullpath']}{err}")


class FerdimApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="app.ferdim.gui")
        self.rows: dict[str, JobRow] = {}
        self.client: FerdimClient | None = None

    # ------------------------------------------------------------- lifecycle
    def do_activate(self):
        env = AppEnv()
        set_gui_lang(env.load_config().get("language"))
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

        self.status = Gtk.Label(label=t("daemon_ok", _LANG))
        self.status.get_style_context().add_class("dim-label")

        self.listbox = Gtk.ListBox(valign=Gtk.Align.START)
        self.listbox.set_placeholder(Gtk.Label(label=t("empty_title", _LANG)))
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_child(self.listbox)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6); box.set_margin_bottom(6)
        box.set_margin_start(8); box.set_margin_end(8)
        box.append(sw)
        box.append(self.status)
        win.set_child(box)
        win.present()

        if ensure_daemon(env):
            self.client = FerdimClient(str(env.socket_path)).connect()
            GLib.timeout_add(500, self.refresh)
            self.refresh()
        else:
            self.status.set_text(t("daemon_down", _LANG))

    # ------------------------------------------------------------- handlers
    def refresh(self):
        if not self.client:
            return True
        try:
            st = self.client.request("stats")
        except Exception:
            self.status.set_text("Bağlantı koptu")
            return True
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
            f"{t('total_speed', _LANG)}: {util.human_size(st['total_speed'])}/s   "
            f"{t('active_jobs', _LANG)}: {st['active']}/{st['max_concurrent']}   "
            f"{t('firefox_ext', _LANG)}: {nm}")
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

    def on_add(self, _btn):
        dlg = Gtk.Dialog(transient_for=self.get_active_window(),
                         modal=True, title=t("h_new", _LANG))
        dlg.add_button(t("btn_cancel", _LANG), Gtk.ResponseType.CANCEL)
        dlg.add_button(t("btn_download", _LANG), Gtk.ResponseType.OK)
        entry = Gtk.Entry(placeholder_text="https://...", hexpand=True)
        seg = Gtk.SpinButton.new_with_range(1, 32, 1)
        seg.set_value(8)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10); box.set_margin_bottom(10)
        box.set_margin_start(14); box.set_margin_end(14)
        box.append(Gtk.Label(label=t("lbl_url", _LANG) + ":", xalign=0))
        box.append(entry)
        box.append(Gtk.Label(label=t("lbl_segments", _LANG) + ":", xalign=0))
        box.append(seg)
        dlg.get_content_area().append(box)
        dlg.connect("response", lambda d, r: self._add_response(d, r, entry, seg))
        dlg.present()

    def _add_response(self, dlg, resp, entry, seg):
        url = entry.get_text().strip()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK or not url.startswith(("http://", "https://")):
            return
        if self.client:
            try:
                self.client.request("add", url=url, segments=int(seg.get_value()))
            except Exception:
                pass
        self.refresh()

    def on_web(self, _btn):
        import subprocess
        env = AppEnv()
        ensure_daemon(env)
        try:
            port = env.port_path.read_text().strip()
            subprocess.Popen(["xdg-open", f"http://127.0.0.1:{port}"])
        except Exception:
            pass


def main() -> int:
    app = FerdimApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
