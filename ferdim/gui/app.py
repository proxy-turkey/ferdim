"""FerDim GTK4 arayuzu (python3-gobject).

PyGObject yoksa net bir Turkce hata verir; arayuzsuz kullanim
(ferdim CLI / web) etkilenmez. Simgeli dugmelere erisilebilir ad, giris
alanlarina animsatici (mnemonic) etiket eklenir; boylece ekran okuyucular
alani adiyla duyurur. GTK 4.10 oncesinde bulunmayan erisilebilirlik
cagrilari sessizce atlanir.
"""
from __future__ import annotations

import re
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
from ..i18n import LANGS, LANG_NAMES
from ..paths import AppEnv
from . import present
from .archive_win import ArchiveWindow
from .history_win import HistoryWindow
from .hosts_win import HostsWindow
from .widgets import (STATES, _a11y, _bind_escape, _button, _check, _field,
                      _grid_field, _mnemonic, _time_entry, _under, gtr, lang,
                      set_gui_lang, tr_state)
# (eylem, i18n anahtari, simge)
ACTIONS = (("resume", "act_resume", "media-playback-start"),
           ("pause", "act_pause", "media-playback-pause"),
           ("restart", "act_restart", "view-refresh"),
           ("archive", "zip_title", "package-x-generic"),
           ("remove", "act_remove", "user-trash"))
# web arayuzuyle ayni kategori kumesi ve hiz on ayarlari
CATEGORIES = ("video", "music", "compressed", "program", "document", "other")
SPEED_PRESETS = (0, 128 * 1024, 512 * 1024, 1024 * 1024,
                 2 * 1024 * 1024, 5 * 1024 * 1024)
_HM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def row_notes(job: dict) -> list[str]:
    """Satirin ikinci satirindaki kisa notlar (sunucu limiti, dogrulama,
    kaynak, zamanlama penceresi). Saglama/Merkle ayrintisi basliktaki
    rozetin ipucundadir."""
    out = []
    if job.get("adaptive") and job.get("adaptive") < job.get("segments_cfg", 0):
        out.append("⚙ " + gtr("adaptive_limit").replace(
            "{n}", str(job["adaptive"])))
    if job.get("verifying"):
        out.append("⏳ " + gtr("st_verifying"))
    if job.get("error"):
        out.append("⚠ " + job["error"])
    if job.get("sched_wait") and job.get("window"):
        out.append("⏱ " + gtr("sched_wait").replace(
            "{w}", job["window"]))
    if job.get("sources_dead"):
        out.append("⛔ " + gtr("mirrors_dead").replace(
            "{n}", str(job["sources_dead"])))
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
        self.badge = Gtk.Label(visible=False)      # saglama/bildirim rozeti
        self.state = Gtk.Label()
        self.state.get_style_context().add_class("dim-label")
        head.append(self.name); head.append(self.badge); head.append(self.state)
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
            tip = gtr(key)
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
        self._show_badge(job)
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
        self.btn["archive"].set_visible(present.is_archive(job))
        parts = [f"{util.human_size(job['done'])} / "
                 f"{util.human_size(job['size'])}"]
        if playing:
            parts.append(f"{util.human_size(job['speed'])}{gtr('per_sec')}"
                         f"   {gtr('col_eta')}: {util.human_eta(job['eta'])}")
        parts.extend(row_notes(job))
        parts.append(job["fullpath"])
        self.info.set_text("   ".join(parts))

    def _show_badge(self, job) -> None:
        """Saglama/Merkle rozeti: simge + renk; ayrinti ipucunda ve ekran
        okuyucu adinda (ok / warn / mismatch)."""
        badge = present.integrity_badge(job, lang())
        if badge is None:
            self.badge.set_visible(False)
            return
        symbol, tip, css = badge
        ctx = self.badge.get_style_context()
        for name in ("success", "warning", "error"):
            ctx.remove_class(name)
        ctx.add_class(css)
        self.badge.set_label(symbol)
        self.badge.set_tooltip_text(tip)
        _a11y(self.badge, "LABEL", [tip])
        self.badge.set_visible(True)


class AddWindow(Gtk.Window):
    """Yeni indirme penceresi.

    Kullanimi onerilmeyen dialog sinifi yerine sade modal pencere
    kullanilir; ek bagimlilik (Adw) gerekmez. Varsayilan dugme, Escape ile
    kapatma ve Tab/Alt+harf dolasimi tam klavye kullanimi verir.
    """

    def __init__(self, app, parent):
        super().__init__(transient_for=parent, modal=True,
                         title=gtr("h_new"), default_width=460)
        self.app = app
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(14); box.set_margin_end(14)

        self.url = Gtk.Entry(placeholder_text="https://...", hexpand=True,
                             activates_default=True)
        _field(box, self.url, "lbl_url")

        self.mirrors = Gtk.TextView(monospace=True,
                                    wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.mirrors.set_left_margin(6); self.mirrors.set_right_margin(6)
        sw = Gtk.ScrolledWindow(height_request=52, hexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER,
                                vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        sw.set_child(self.mirrors)
        _field(box, sw, "lbl_mirrors", target=self.mirrors)

        self.seg = Gtk.SpinButton.new_with_range(1, 32, 1)
        try:    # sunucu varsayilani (turbo'da 32, degilse config'ten)
            self.seg.set_value(int(app.client.request("ping").get("segments")
                                   or 8))
        except Exception:
            self.seg.set_value(8)
        _field(box, self.seg, "lbl_segments")

        self.chk = Gtk.Entry(placeholder_text=gtr("ph_checksum"),
                             hexpand=True)
        _field(box, self.chk, "lbl_checksum")

        self.now = _check("start_now", True)
        box.append(self.now)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_halign(Gtk.Align.END)
        actions.set_margin_top(6)
        self.cancel = _button("btn_cancel")
        self.cancel.connect("clicked", lambda _b: self.close())
        self.ok = _button("btn_download", suggested=True)
        self.ok.connect("clicked", self.submit)
        actions.append(self.cancel); actions.append(self.ok)
        box.append(actions)

        self.set_child(box)
        self.set_default_widget(self.ok)
        _bind_escape(self)

    def submit(self, _btn=None):
        """Dogrular; hata penceresi acik kalir, basarida pencere kapanir."""
        url = self.url.get_text().strip()
        if not url.lower().startswith(("http://", "https://", "ftp://", "ftps://")):
            self.app._error(gtr("err_url"), self)
            return
        digest = self.chk.get_text().strip()
        if digest and not util.parse_checksum_spec(digest)[0]:
            self.app._error(gtr("cli_bad_checksum"), self)
            return
        buf = self.mirrors.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        mirrors = [u for u in text.split()
                   if u.startswith(("http://", "https://"))][:24]
        r = {"ok": True}
        if self.app.client:
            try:
                r = self.app.client.request(
                    "add", url=url, segments=int(self.seg.get_value()),
                    checksum=digest, mirrors=mirrors,
                    paused=not self.now.get_active())
            except Exception as e:
                r = {"ok": False, "error": str(e)}
        if not r.get("ok"):
            self.app._error(gtr("add_failed") + " "
                            + str(r.get("error", "")), self)
            return
        self.close()
        self.app.refresh()


class SettingsWindow(Gtk.Window):
    """Ayarlar penceresi: web arayuzunun ayar penceresiyle ayni alanlar
    (indirme dizini, dil, baglanti/eszamanli sayisi, turbo, hiz limiti,
    zamanlama + kategori pencereleri, izlenen klasorler, pano izleme,
    saglama ve gelismis yeniden deneme/zaman asimi degerleri)."""

    def __init__(self, app, parent):
        super().__init__(transient_for=parent, modal=True,
                         title=gtr("h_settings"), default_width=560,
                         default_height=660)
        self.app = app
        cfg = app.fetch_config()
        self.cfg0 = dict(cfg)        # karsilastirma tabani (acilis anindaki)
        self.initial_lang = cfg.get("language") or "auto"
        self.initial_limit = int(cfg.get("speed_limit") or 0)
        self.initial_turbo = bool(cfg.get("turbo_mode"))
        # turbo kapaninca daemon'in geri yukleyecegi limit: turbo_prev
        prev = cfg.get("turbo_prev") or {}
        self._limit_before_turbo = (
            int(prev.get("speed_limit", self.initial_limit) or 0)
            if self.initial_turbo else self.initial_limit)
        self._applying = False
        sch = cfg.get("schedule") or {}
        cats = sch.get("categories") or {}

        grid = Gtk.Grid(column_spacing=12, row_spacing=8,
                        margin_top=12, margin_bottom=12,
                        margin_start=14, margin_end=14)
        row = 0
        self.dir = Gtk.Entry(text=cfg.get("download_dir") or "",
                             placeholder_text=gtr("auto"))
        _grid_field(grid, row, self.dir, "lbl_dl_dir"); row += 1

        self.lang_codes = ["auto"] + list(LANGS)
        names = [gtr("lang_auto")] + [LANG_NAMES[k] for k in LANGS]
        cur = str(self.initial_lang)
        idx = self.lang_codes.index(cur) if cur in self.lang_codes else 0
        self.lang = Gtk.DropDown.new_from_strings(names)
        self.lang.set_selected(idx)
        _grid_field(grid, row, self.lang, "lbl_lang"); row += 1

        self.speeds = list(SPEED_PRESETS)
        if self.initial_limit not in self.speeds:
            self.speeds.append(self.initial_limit)
        self.limit = Gtk.DropDown.new_from_strings(self._limit_labels())
        self.limit.set_selected(self.speeds.index(self.initial_limit))
        _grid_field(grid, row, self.limit, "lbl_speed"); row += 1

        self.seg = Gtk.SpinButton.new_with_range(1, 32, 1)
        self.seg.set_value(int(cfg.get("segments") or 8))
        _grid_field(grid, row, self.seg, "lbl_segments"); row += 1
        self.conc = Gtk.SpinButton.new_with_range(1, 32, 1)
        self.conc.set_value(int(cfg.get("max_concurrent") or 8))
        _grid_field(grid, row, self.conc, "lbl_conc"); row += 1

        self.turbo = _check("turbo", self.initial_turbo)
        self.turbo.connect("toggled", self._on_turbo)
        grid.attach(self.turbo, 0, row, 2, 1); row += 1
        self._sync_limit()
        self.sched_on = _check("lbl_sched_only", bool(sch.get("enabled")))
        grid.attach(self.sched_on, 0, row, 2, 1); row += 1

        start = str(sch.get("start") or "")
        stop = str(sch.get("stop") or "")
        self.sched_start = _time_entry(
            start if _HM.match(start) else "02:00")
        _grid_field(grid, row, self.sched_start, "lbl_sched_start"); row += 1
        self.sched_stop = _time_entry(stop if _HM.match(stop) else "08:00")
        _grid_field(grid, row, self.sched_stop, "lbl_sched_stop"); row += 1

        frame = Gtk.Frame(label=gtr("lbl_cat_sched"))
        cbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        cbox.set_margin_top(6); cbox.set_margin_bottom(6)
        cbox.set_margin_start(8); cbox.set_margin_end(8)
        self.cat_rows = {}
        for cat in CATEGORIES:
            w = cats.get(cat) or {}
            name = gtr("tab_" + cat)
            on = w.get("start") and w.get("stop")
            cb = Gtk.CheckButton(use_underline=True, active=bool(on))
            cb.set_label(_under(name))
            a = _time_entry(w.get("start") or "02:00",
                            name + " — " + gtr("win_start"))
            b = _time_entry(w.get("stop") or "08:00",
                            name + " — " + gtr("win_stop"))
            a.set_sensitive(bool(on)); b.set_sensitive(bool(on))
            cb.connect("toggled", lambda c, x=a, y=b:
                       (x.set_sensitive(c.get_active()),
                        y.set_sensitive(c.get_active())))
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            line.append(cb)
            line.append(a)
            line.append(Gtk.Label(label="–"))
            line.append(b)
            cbox.append(line)
            self.cat_rows[cat] = (cb, a, b)
        frame.set_child(cbox)
        grid.attach(frame, 0, row, 2, 1); row += 1

        self.watch_view = Gtk.TextView(monospace=True,
                                       wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.watch_view.get_buffer().set_text(
            "\n".join(cfg.get("watch_dirs") or []))
        wsw = Gtk.ScrolledWindow(height_request=56, hexpand=True,
                                 hscrollbar_policy=Gtk.PolicyType.NEVER,
                                 vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        wsw.set_child(self.watch_view)
        _grid_field(grid, row, wsw, "lbl_watch_dirs",
                    target=self.watch_view); row += 1

        self.watch_modes = ["delete", "keep", "rename"]
        self.watch_done = Gtk.DropDown.new_from_strings(
            [gtr("watch_delete"), gtr("watch_keep"),
             gtr("watch_rename")])
        self.watch_done.set_selected(
            self.watch_modes.index(cfg.get("watch_done"))
            if cfg.get("watch_done") in self.watch_modes else 0)
        _grid_field(grid, row, self.watch_done, "lbl_watch_done"); row += 1

        self.sidecar = _check("lbl_sidecar", bool(cfg.get("checksum_sidecar")))
        grid.attach(self.sidecar, 0, row, 2, 1); row += 1
        self.clipboard = _check("lbl_clipboard",
                                bool(cfg.get("clipboard_watch")))
        grid.attach(self.clipboard, 0, row, 2, 1); row += 1
        self.coexist = _check("lbl_coexist", bool(cfg.get("coexist")))
        self.coexist.set_tooltip_text(gtr("coexist_hint"))
        grid.attach(self.coexist, 0, row, 2, 1); row += 1

        adv = Gtk.Expander(label=gtr("lbl_advanced"))
        ag = Gtk.Grid(column_spacing=12, row_spacing=6, margin_top=6)
        self.retries = Gtk.SpinButton.new_with_range(0, 20, 1)
        self.retries.set_value(int(cfg.get("retries") or 0))
        _grid_field(ag, 0, self.retries, "lbl_retries")
        self.timeout = Gtk.SpinButton.new_with_range(5, 300, 5)
        self.timeout.set_value(int(cfg.get("timeout") or 30))
        self.timeout.connect("value-changed", self._sync_stall)
        _grid_field(ag, 1, self.timeout, "lbl_timeout")
        self.stall = Gtk.SpinButton.new_with_range(5, 120, 5)
        self.stall.set_value(int(cfg.get("stall_timeout") or 20))
        _grid_field(ag, 2, self.stall, "lbl_stall")
        adv.set_child(ag)
        grid.attach(adv, 0, row, 2, 1); row += 1

        scrolled = Gtk.ScrolledWindow(vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(grid)

        self.status = Gtk.Label(label="", xalign=0, hexpand=True,
                                ellipsize=2)
        self.status.get_style_context().add_class("dim-label")
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_margin_top(6); actions.set_margin_bottom(10)
        actions.set_margin_start(14); actions.set_margin_end(14)
        self.close_btn = _button("btn_close")
        self.close_btn.connect("clicked", lambda _b: self.close())
        self.save_btn = _button("btn_save", suggested=True)
        self.save_btn.connect("clicked", self.save)
        actions.append(self.status)
        actions.append(self.close_btn); actions.append(self.save_btn)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(scrolled)
        outer.append(actions)
        self.set_child(outer)
        _bind_escape(self)

    # --------------------------------------------------------------- yardimci
    def _limit_labels(self) -> list[str]:
        out = [gtr("unlimited")]
        for v in self.speeds[1:]:
            label = util.human_size(v) + gtr("per_sec")
            if v not in SPEED_PRESETS:
                label += " (" + gtr("limit_custom") + ")"
            out.append(label)
        return out

    def _set_limit(self, value: int) -> None:
        """Secimi verilen degere getirir; on ayarda olmayan deger icin
        listeye 'ozel' satiri ekler (web arayuzuyle ayni davranis)."""
        if value not in self.speeds:
            self.speeds.append(value)
            self.limit.set_model(Gtk.StringList.new(self._limit_labels()))
        self.limit.set_selected(self.speeds.index(value))

    def _current_limit(self) -> int:
        idx = self.limit.get_selected()
        return self.speeds[idx] if idx < len(self.speeds) else 0

    def _sync_limit(self) -> None:
        """Turbo acikken hiz limiti yoktur: alan etkisiz ve limitsiz
        gorunur; saklanan secim turbo kapaninca geri gelir."""
        turbo = self.turbo.get_active()
        if turbo:
            self.limit.set_selected(0)
        self.limit.set_sensitive(not turbo)

    def _on_turbo(self, btn) -> None:
        """Kullanici turbo'yu acti/kapatti: limit secimini sakla ya da
        geri getir (yapilandirma uygulanirken sinyal yok sayilir)."""
        if self._applying:
            return
        if btn.get_active():
            self._limit_before_turbo = self._current_limit()
            self.limit.set_selected(0)
            self.limit.set_sensitive(False)
        else:
            self._set_limit(self._limit_before_turbo)
            self.limit.set_sensitive(True)

    def _sync_stall(self, _spin) -> None:
        """stall_timeout daemon tarafinda timeout'a kirpilir; ayni kirpmayi
        GUI'de uygula ki kaydedilen deger ekranda gorunenle ayni olsun."""
        limit = int(self.timeout.get_value())
        if int(self.stall.get_value()) > limit:
            self.stall.set_value(limit)

    def _unchanged(self, key: str, value) -> bool:
        """Kullanici bu alani degistirdi mi? (taban: pencerenin actigi
        andaki yapilandirma)"""
        old = self.cfg0.get(key)
        if key == "schedule":
            old = old or {}
            return (bool(old.get("enabled")) == value["enabled"]
                    and str(old.get("start") or "") == value["start"]
                    and str(old.get("stop") or "") == value["stop"]
                    and (old.get("categories") or {}) == value["categories"])
        if key == "download_dir":
            return (old or None) == value
        if key == "watch_dirs":
            return list(old or []) == value
        return old == value

    def _apply_config(self, cfg: dict) -> None:
        """Sunucudaki etkin degerleri pencereye yansitir ve yeni taban
        olarak saklar (kirpilan degerler boylece gorunur)."""
        self.cfg0 = dict(cfg)
        self.initial_lang = cfg.get("language") or "auto"
        self.initial_turbo = bool(cfg.get("turbo_mode"))
        self.initial_limit = int(cfg.get("speed_limit") or 0)
        sch = cfg.get("schedule") or {}
        cats = sch.get("categories") or {}
        self.dir.set_text(cfg.get("download_dir") or "")
        cur = str(self.initial_lang)
        self.lang.set_selected(self.lang_codes.index(cur)
                               if cur in self.lang_codes else 0)
        self.seg.set_value(int(cfg.get("segments") or 8))
        self.conc.set_value(int(cfg.get("max_concurrent") or 8))
        prev = cfg.get("turbo_prev") or {}
        self._limit_before_turbo = (
            int(prev.get("speed_limit", self.initial_limit) or 0)
            if self.initial_turbo else self.initial_limit)
        self._applying = True
        try:      # toggled sinyali saklanan secimi ezmesin
            self.turbo.set_active(self.initial_turbo)
        finally:
            self._applying = False
        self._set_limit(0 if self.initial_turbo else self.initial_limit)
        self._sync_limit()
        self.sched_on.set_active(bool(sch.get("enabled")))
        start = str(sch.get("start") or "")
        stop = str(sch.get("stop") or "")
        self.sched_start.set_text(start if _HM.match(start) else "02:00")
        self.sched_stop.set_text(stop if _HM.match(stop) else "08:00")
        for cat, (cb, a, b) in self.cat_rows.items():
            w = cats.get(cat) or {}
            on = bool(w.get("start") and w.get("stop"))
            cb.set_active(on)
            a.set_text(w.get("start") or "02:00")
            b.set_text(w.get("stop") or "08:00")
            a.set_sensitive(on); b.set_sensitive(on)
        self.watch_view.get_buffer().set_text(
            "\n".join(cfg.get("watch_dirs") or []))
        self.watch_done.set_selected(
            self.watch_modes.index(cfg.get("watch_done"))
            if cfg.get("watch_done") in self.watch_modes else 0)
        self.sidecar.set_active(bool(cfg.get("checksum_sidecar")))
        self.clipboard.set_active(bool(cfg.get("clipboard_watch")))
        self.coexist.set_active(bool(cfg.get("coexist")))
        self.retries.set_value(int(cfg.get("retries") or 0))
        self.timeout.set_value(int(cfg.get("timeout") or 30))
        self.stall.set_value(int(cfg.get("stall_timeout") or 20))

    def save(self, _btn=None):
        start = self.sched_start.get_text().strip()
        stop = self.sched_stop.get_text().strip()
        if not (_HM.match(start) and _HM.match(stop)):
            self.app._error(gtr("err_time"), self)
            return
        cats = {}
        for cat, (cb, a, b) in self.cat_rows.items():
            if not cb.get_active():
                continue
            w_start = a.get_text().strip()
            w_stop = b.get_text().strip()
            if not (_HM.match(w_start) and _HM.match(w_stop)):
                self.app._error(gtr("err_time"), self)
                return
            cats[cat] = {"start": w_start, "stop": w_stop}
        buf = self.watch_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        watch_dirs = [s.strip() for s in text.splitlines() if s.strip()]
        limit = self.speeds[self.limit.get_selected()]
        lang = self.lang_codes[self.lang.get_selected()]
        turbo_want = bool(self.turbo.get_active())
        timeout = int(self.timeout.get_value())
        # api stall_timeout'u timeout'a kirpar: ayni kirpmayi burada yap
        stall = min(int(self.stall.get_value()), timeout)
        cand = {
            "download_dir": self.dir.get_text().strip() or None,
            "language": lang,
            "segments": int(self.seg.get_value()),
            "max_concurrent": int(self.conc.get_value()),
            "schedule": {"enabled": self.sched_on.get_active(),
                         "start": start, "stop": stop,
                         "categories": cats},
            "watch_dirs": watch_dirs,
            "watch_done": self.watch_modes[self.watch_done.get_selected()],
            "checksum_sidecar": self.sidecar.get_active(),
            "clipboard_watch": self.clipboard.get_active(),
            "coexist": self.coexist.get_active(),
            "retries": int(self.retries.get_value()),
            "timeout": timeout,
            "stall_timeout": stall,
        }
        # yalniz kullanicinin gercekten degistirdigi alanlar gonderilir:
        # turbo acikken sisirilmis max_concurrent turbo_prev'e sizmaz
        cfg = {k: v for k, v in cand.items() if not self._unchanged(k, v)}
        notes = []
        if self.app.client:
            try:
                if cfg:
                    r = self.app.client.request("config-set", config=cfg)
                    if not r.get("ok"):
                        raise RuntimeError(str(r.get("error", "")))
                    if r.get("warning") == "download_dir":
                        notes.append(gtr("warn_dir"))
                turbo_changed = turbo_want != self.initial_turbo
                if turbo_changed:
                    self.app.client.request("turbo", on=turbo_want)
                # turbo ve limit asla celismez: turbo acikken limit komutu
                # gonderilmez; secilen limit yalniz turbo kapaliyken uygulanir
                # (turbo kapaninca kendi onceki limitini geri yukler)
                if not turbo_want and limit != self.initial_limit:
                    self.app.client.request("limit", value=limit)
                cfg1 = self.app.fetch_config()
            except Exception as e:
                self.app._error(gtr("save_failed") + " " + str(e), self)
                return
            lang_changed = lang != self.initial_lang
            self._apply_config(cfg1)     # etkin (kirpilmis) degerler ekranda
            if lang_changed:
                # simdilik yalniz yeni pencereler yeni dili kullanir
                set_gui_lang(lang)
                notes.append(gtr("lang_restart"))
        self.status.set_text(" ".join(notes) if notes else gtr("saved"))
        self.app.refresh()


class FerdimApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="app.ferdim.gui")
        self.rows: dict[str, JobRow] = {}
        self.client: FerdimClient | None = None
        self.env = AppEnv()
        self.failures = 0
        self.win: Gtk.Window | None = None
        self.add_win: AddWindow | None = None
        self.settings_win: SettingsWindow | None = None
        self.archive_win: ArchiveWindow | None = None
        self.history_win: HistoryWindow | None = None
        self.hosts_win: HostsWindow | None = None

    # ------------------------------------------------------------- lifecycle
    def do_activate(self):
        set_gui_lang(self.env.load_config().get("language"))
        win = Gtk.ApplicationWindow(application=self)
        self.win = win
        win.set_title(f"FerDim {__version__} — {gtr('app_title')}")
        win.set_default_size(780, 480)

        hb = Gtk.HeaderBar()
        win.set_titlebar(hb)

        add_btn = Gtk.Button(label=gtr("h_new"))
        add_btn.get_style_context().add_class("suggested-action")
        add_btn.connect("clicked", self.on_add)
        hb.pack_start(add_btn)

        self.turbo_btn = Gtk.Button(label="⚡ " + gtr("turbo"),
                                    tooltip_text=gtr("turbo_hint"))
        self.turbo_btn.connect("clicked", self.on_turbo)
        hb.pack_start(self.turbo_btn)

        hist_btn = Gtk.Button(label=gtr("hist_title"))
        hist_btn.connect("clicked", self.on_history)
        hb.pack_start(hist_btn)

        hosts_btn = Gtk.Button(label=gtr("hosts_title"))
        hosts_btn.connect("clicked", self.on_hosts)
        hb.pack_start(hosts_btn)

        set_btn = Gtk.Button(label=gtr("h_settings"))
        set_btn.connect("clicked", self.on_settings)
        hb.pack_end(set_btn)

        web_btn = Gtk.Button(label=gtr("web_ui"))
        web_btn.connect("clicked", self.on_web)
        hb.pack_end(web_btn)

        self.status = Gtk.Label(label=gtr("daemon_ok"), xalign=0,
                                ellipsize=2)
        self.status.get_style_context().add_class("dim-label")
        self.status.set_margin_start(8); self.status.set_margin_end(8)
        # birlikte yasam durumu (stats.coexist): hat mesgulse baglanti kisilir
        self.coexist_lbl = Gtk.Label(label="", xalign=0, ellipsize=2)
        self.coexist_lbl.get_style_context().add_class("dim-label")
        self.coexist_lbl.set_margin_start(8); self.coexist_lbl.set_margin_end(8)

        self.listbox = Gtk.ListBox(valign=Gtk.Align.START,
                                   selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.set_placeholder(Gtk.Label(label=gtr("empty_title")))
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_child(self.listbox)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6); box.set_margin_bottom(6)
        box.append(sw)
        box.append(self.coexist_lbl)
        box.append(self.status)
        win.set_child(box)
        win.present()

        if ensure_daemon(self.env):
            self.connect_client()
            GLib.timeout_add(500, self.refresh)
            self.refresh()
        else:
            self.status.set_text(gtr("daemon_down"))

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
            self.status.set_text(gtr("daemon_lost"))
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
        nm = gtr("nm_ok") if st.get("nm_connected") else gtr("nm_off")
        self.status.set_text(
            f"{gtr('total_speed')}: "
            f"{util.human_size(st['total_speed'])}{gtr('per_sec')}   "
            f"{gtr('active_jobs')}: {st['active']}/{st['max_concurrent']}   "
            f"{gtr('ext_state')}: {nm}")
        self._show_coexist(st.get("coexist") or {})
        turbo = bool(st.get("turbo"))
        self.turbo_btn.set_label(
            "⚡ " + (gtr("turbo_on") if turbo else gtr("turbo")))
        ctx = self.turbo_btn.get_style_context()
        if turbo:
            ctx.add_class("destructive-action")
        else:
            ctx.remove_class("destructive-action")
        return True

    def _show_coexist(self, co: dict) -> None:
        """stats.coexist: mod acik mi, hat mesgulse baglanti kisildi mi."""
        on = bool(co.get("enabled"))
        text = (f"{gtr('coexist')}: "
                + (gtr("coexist_on") if on else gtr("coexist_off")))
        active = on and bool(co.get("active"))
        if active:
            text += " — " + gtr("coexist_active")
        self.coexist_lbl.set_text(text)
        ctx = self.coexist_lbl.get_style_context()
        if active:
            ctx.add_class("warning")
        else:
            ctx.remove_class("warning")

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
        elif action == "archive":
            self.on_archive(job_id)
        else:
            self._do_action(job_id, action)

    def on_archive(self, job_id):
        win = ArchiveWindow(self, self._parent_window(), {"id": job_id})
        self.archive_win = win
        win.present()

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
        self.confirm(gtr("confirm_remove"),
                     lambda: self._do_action(job_id, "remove"),
                     gtr("act_remove"))

    def confirm(self, message: str, on_yes, yes_label: str) -> None:
        """Genel onay penceresi (silme, profilleri unutma). GTK < 4.10'da
        kapanis yoktur: eylem dogrudan uygulanir."""
        win = self.get_active_window()
        if not hasattr(Gtk, "AlertDialog"):
            on_yes()
            return
        ad = Gtk.AlertDialog(message=message,
                             buttons=[gtr("btn_cancel"), yes_label])
        ad.set_cancel_button(0)
        ad.set_default_button(1)
        ad.choose(win, None, lambda d, res: self._confirm_done(d, res, on_yes))

    def _confirm_done(self, dialog, res, on_yes):
        try:
            if dialog.choose_finish(res) == 1:
                on_yes()
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

    def _parent_window(self):
        return self.get_active_window() or self.win

    def fetch_config(self) -> dict:
        """Canli yapilandirma; daemon yoksa yapilandirma dosyasi."""
        if self.client:
            try:
                r = self.client.request("config-get")
                if r.get("ok") and isinstance(r.get("config"), dict):
                    return r["config"]
            except Exception:
                pass
        return self.env.load_config()

    def on_add(self, _btn):
        win = AddWindow(self, self._parent_window())
        self.add_win = win
        win.present()

    def on_settings(self, _btn):
        win = SettingsWindow(self, self._parent_window())
        self.settings_win = win
        win.present()

    def on_history(self, _btn):
        win = HistoryWindow(self, self._parent_window())
        self.history_win = win
        win.present()

    def on_hosts(self, _btn):
        win = HostsWindow(self, self._parent_window())
        self.hosts_win = win
        win.present()

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
