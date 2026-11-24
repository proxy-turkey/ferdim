"""Arsiv penceresi: ic ice listeleme, secmeli cikarma ve onarim.

Bitmis bir arsiv isinin icerigi daemon'dan alinir (archive-list) ve cikarma
arka planda kosar; ilerleme archive-status yoklamasiyla gelir. Istekler
ayri baglantida kosar (widgets.run_async): ana dongu donmaz, kanal
karismaz.
"""
from __future__ import annotations

from gi.repository import GLib, Gtk

from .. import util
from .widgets import (_a11y, _bind_escape, _button, gtr, request_async,
                      scrolled)

MAX_ROWS = 400          # web arayuzuyle ayni gorunum siniri
POLL_MS = 400           # archive-status yoklama araligi


class ArchiveWindow(Gtk.Window):
    def __init__(self, app, parent, job):
        super().__init__(transient_for=parent, modal=False,
                         title=gtr("zip_title"), default_width=580,
                         default_height=560)
        self.app = app
        self.job_id = job.get("id", "")
        self.task = ""
        self.checks: list[tuple[Gtk.CheckButton, str]] = []
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10); box.set_margin_bottom(10)
        box.set_margin_start(12); box.set_margin_end(12)

        self.head = Gtk.Label(xalign=0, ellipsize=3)
        self.head.get_style_context().add_class("heading")
        self.head.set_text(job.get("filename") or self.job_id)
        box.append(self.head)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.set_placeholder(Gtk.Label(label=gtr("zip_empty")))
        box.append(scrolled(self.listbox))

        self.status = Gtk.Label(label="", xalign=0, ellipsize=2,
                                selectable=True, wrap=True)
        self.status.get_style_context().add_class("dim-label")
        box.append(self.status)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_margin_top(4)
        self.repair_btn = _button("zip_repair")
        self.repair_btn.connect("clicked", self.repair)
        self.close_btn = _button("btn_close")
        self.close_btn.connect("clicked", lambda _b: self.close())
        self.extract_btn = _button("zip_extract", suggested=True)
        self.extract_btn.connect("clicked", self.extract)
        actions.append(self.repair_btn)
        actions.append(Gtk.Box(hexpand=True))
        actions.append(self.close_btn)
        actions.append(self.extract_btn)
        box.append(actions)

        self.set_child(box)
        _bind_escape(self)
        self.load()

    # ----------------------------------------------------------------- yukle
    def load(self):
        request_async(self.app, "archive-list", self._loaded,
                      id=self.job_id, nested=True)

    def _loaded(self, res):
        try:
            if not res.get("ok"):
                self._status(gtr("zip_title") + ": "
                             + str(res.get("error") or ""))
                return False
            entries = [e for e in (res.get("entries") or [])
                       if e.get("kind") == "file"]
            name = res.get("filename") or self.head.get_text()
            self.head.set_text(f"{name} ({len(entries)}{res.get('note') or ''})")
            self.checks = []
            child = self.listbox.get_first_child()
            while child is not None:            # yeniden yuklemede temizle
                nxt = child.get_next_sibling()
                self.listbox.remove(child)
                child = nxt
            for e in entries[:MAX_ROWS]:
                self.listbox.append(self._row(e))
            if not entries:
                self._status(gtr("zip_empty"))
        except Exception as e:                  # pencere kapanmis olabilir
            self._status(str(e))
        return False

    def _row(self, entry) -> Gtk.ListBoxRow:
        path = str(entry.get("path") or entry.get("key") or "")
        row = Gtk.ListBoxRow()
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        line.set_margin_top(3); line.set_margin_bottom(3)
        line.set_margin_end(6)
        depth = max(1, int(entry.get("depth") or 1))
        line.set_margin_start(4 + (depth - 1) * 14)
        cb = Gtk.CheckButton()
        cb.set_active(True)
        _a11y(cb, "LABEL", [gtr("zip_select") + ": " + path])
        name = Gtk.Label(label=path, xalign=0, hexpand=True, ellipsize=3)
        size = Gtk.Label(label=util.human_size(entry.get("size") or 0),
                         xalign=1)
        size.get_style_context().add_class("dim-label")
        line.append(cb); line.append(name); line.append(size)
        row.set_child(line)
        self.checks.append((cb, str(entry.get("key") or path)))
        return row

    # --------------------------------------------------------------- eylemler
    def _status(self, text: str) -> None:
        try:
            self.status.set_text(text)
        except Exception:
            pass

    def _busy(self, busy: bool) -> None:
        for b in (self.extract_btn, self.repair_btn):
            b.set_sensitive(not busy)

    def extract(self, _btn=None):
        if self.task:
            return
        members = [key for cb, key in self.checks if cb.get_active()]
        if not members:
            self._status(gtr("zip_sel_none"))
            return
        self._busy(True)
        request_async(self.app, "archive-extract", self._started,
                      id=self.job_id, members=members)

    def repair(self, _btn=None):
        if self.task:
            return
        self._busy(True)
        request_async(self.app, "archive-repair", self._started,
                      id=self.job_id)

    def _started(self, res):
        try:
            if not res.get("ok"):
                self._busy(False)
                self._status(gtr("zip_nomf") if res.get("code") == "no_manifest"
                             else gtr("zip_failed",
                                      e=str(res.get("error") or "")))
                return False
            self.task = str(res.get("task") or "")
            self._status(gtr("zip_running", done=0, total=0))
            self._poll()
        except Exception:
            pass
        return False

    # --------------------------------------------------------------- izleme
    def _poll(self):
        task = self.task
        if not task:
            return False
        request_async(self.app, "archive-status", self._polled, task=task)
        return False

    def _polled(self, st):
        try:
            if not st.get("ok"):
                self.task = ""
                self._busy(False)
                self._status(gtr("zip_failed",
                                 e=str(st.get("error") or "")))
                return False
            if st.get("state") == "running":
                self._status(gtr("zip_running", done=st.get("done") or 0,
                                 total=st.get("total") or 0))
                GLib.timeout_add(POLL_MS, self._poll)
                return False
            self.task = ""
            self._busy(False)
            self._done(st)
        except Exception:
            pass
        return False

    def _done(self, st) -> None:
        res = st.get("result") or {}
        errs = res.get("errors") or []
        if st.get("state") == "done" and res.get("ok"):
            key = "zip_repaired" if res.get("repair") else "zip_extracted"
            text = gtr(key, n=res.get("extracted") or 0,
                       dest=res.get("dest") or "")
            if errs:
                text += " — " + gtr("zip_errors", n=len(errs), e=errs[0])
        else:
            text = gtr("zip_failed", e=st.get("error")
                       or (errs[0] if errs else ""))
        self._status(text)
