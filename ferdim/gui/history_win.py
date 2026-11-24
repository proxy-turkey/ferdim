"""Gecmis penceresi: arama, durum suzgeci, yeniden indirme ve ozet.

Gecmis daemon'daki SQLite kaydindan gelir (history / history-stats);
yeniden indirme history-redownload ile kuyruga yeni is ekler. Istekler
ayri baglantida kosar (widgets.run_async).
"""
from __future__ import annotations

from gi.repository import Gtk

from . import present
from .widgets import (_a11y, _bind_escape, _button, _icon_button, _mnemonic,
                      gtr, lang, request_async, scrolled, tr_state)

STATE_CODES = ("", "done", "error", "canceled")
LIST_LIMIT = 100


class HistoryWindow(Gtk.Window):
    def __init__(self, app, parent):
        super().__init__(transient_for=parent, modal=False,
                         title=gtr("hist_title"), default_width=680,
                         default_height=560)
        self.app = app
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10); box.set_margin_bottom(10)
        box.set_margin_start(12); box.set_margin_end(12)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.search = Gtk.Entry(placeholder_text=gtr("hist_search_ph"),
                                hexpand=True)
        self.search.connect("activate", self.load)
        self.state = Gtk.DropDown.new_from_strings(self._state_labels())
        self.state.set_selected(0)
        self.state.connect("notify::selected", self.load)
        find = _icon_button("system-search", gtr("hist_search"))
        find.connect("clicked", self.load)
        top.append(_mnemonic("hist_search", self.search))
        top.append(self.search)
        top.append(find)
        top.append(_mnemonic("hist_state", self.state))
        top.append(self.state)
        box.append(top)

        self.stats = Gtk.Label(label="", xalign=0, ellipsize=2, wrap=True)
        self.stats.get_style_context().add_class("dim-label")
        box.append(self.stats)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.set_placeholder(Gtk.Label(label=gtr("hist_empty")))
        _a11y(self.listbox, "LABEL", [gtr("hist_title")])
        box.append(scrolled(self.listbox))
        self.recs: list[tuple[Gtk.ListBoxRow, dict]] = []   # (satir, kayit)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_margin_top(4)
        self.status = Gtk.Label(label="", xalign=0, hexpand=True, ellipsize=2)
        self.status.get_style_context().add_class("dim-label")
        self.close_btn = _button("btn_close")
        self.close_btn.connect("clicked", lambda _b: self.close())
        actions.append(self.status)
        actions.append(self.close_btn)
        box.append(actions)

        self.set_child(box)
        _bind_escape(self)
        self.load()

    # --------------------------------------------------------------- yukleme
    def _state_labels(self) -> list[str]:
        return [gtr("hist_all")] + [tr_state(code) for code in STATE_CODES[1:]]

    def load(self, *_a):
        text = self.search.get_text().strip()
        idx = self.state.get_selected()
        state = STATE_CODES[idx] if 0 <= idx < len(STATE_CODES) else ""
        request_async(self.app, "history", self._loaded,
                      q=text, state=state, limit=LIST_LIMIT)
        request_async(self.app, "history-stats", self._stats)
        return False

    def _loaded(self, res):
        try:
            child = self.listbox.get_first_child()
            while child is not None:            # onceki sonucu temizle
                nxt = child.get_next_sibling()
                self.listbox.remove(child)
                child = nxt
            self.recs = []
            if not res.get("ok"):
                self._say(gtr("hist_failed"))
                return False
            for rec in res.get("items") or []:
                row = self._row(rec)
                self.listbox.append(row)
                self.recs.append((row, rec))
            if not res.get("items"):
                self._say(gtr("hist_none")
                          if self.search.get_text().strip()
                          or self.state.get_selected() > 0
                          else gtr("hist_empty"))
        except Exception:
            pass
        return False

    def _stats(self, res):
        try:
            self.stats.set_text(present.history_stats_line(res, lang())
                                if res.get("ok") else gtr("hist_failed"))
        except Exception:
            pass
        return False

    def _row(self, rec) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(5); box.set_margin_bottom(5)
        box.set_margin_start(6); box.set_margin_end(6)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        name = rec.get("filename") or rec.get("url") or rec.get("id", "")
        title = Gtk.Label(label=name, xalign=0, hexpand=True, ellipsize=3)
        title.get_style_context().add_class("heading")
        tip = gtr("hist_redownload")
        again = _icon_button("view-refresh", tip + ": " + name)
        again.connect("clicked", self._redownload, str(rec.get("id") or ""))
        head.append(title); head.append(again)
        url = Gtk.Label(label=rec.get("url") or "", xalign=0, ellipsize=2,
                        selectable=True)
        meta = Gtk.Label(label=present.history_meta(rec, lang()),
                         xalign=0, ellipsize=2)
        links = Gtk.Label(label=present.history_links(rec, lang()),
                          xalign=0)
        for w in (url, meta, links):
            w.get_style_context().add_class("dim-label")
        box.append(head); box.append(url); box.append(meta); box.append(links)
        row.set_child(box)
        return row

    # --------------------------------------------------------------- eylemler
    def _say(self, text: str) -> None:
        try:
            self.status.set_text(text)
        except Exception:
            pass

    def _redownload(self, _btn, rec_id: str):
        if not rec_id:
            return
        request_async(self.app, "history-redownload", self._redownloaded,
                      id=rec_id)

    def _redownloaded(self, res):
        try:
            if not res.get("ok"):
                self._say(gtr("add_failed") + " "
                          + str(res.get("error") or ""))
                return False
            self._say(gtr("hist_redownloaded",
                          name=res.get("filename") or ""))
            self.app.refresh()
        except Exception:
            pass
        return False
