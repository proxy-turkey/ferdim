"""Ogrenilen ana makine profilleri penceresi: listele / unut / tumunu sil.

Profiller daemon'daki hosts.json'dan gelir ('hosts' komutu); 'forget' tek
kaydi, 'clear' tumunu siler. Yeni isler bu profillerden baglanti sayisi
tohumu alir.
"""
from __future__ import annotations

from gi.repository import Gtk

from . import present
from .widgets import (_a11y, _bind_escape, _button, _icon_button, gtr, lang,
                      request_async, scrolled)


class HostsWindow(Gtk.Window):
    def __init__(self, app, parent):
        super().__init__(transient_for=parent, modal=False,
                         title=gtr("hosts_title"), default_width=560,
                         default_height=420)
        self.app = app
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10); box.set_margin_bottom(10)
        box.set_margin_start(12); box.set_margin_end(12)

        head = Gtk.Label(label=gtr("hosts_title"), xalign=0)
        head.get_style_context().add_class("heading")
        box.append(head)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.set_placeholder(Gtk.Label(label=gtr("hosts_empty")))
        _a11y(self.listbox, "LABEL", [gtr("hosts_title")])
        box.append(scrolled(self.listbox))

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_margin_top(4)
        self.status = Gtk.Label(label="", xalign=0, hexpand=True, ellipsize=2)
        self.status.get_style_context().add_class("dim-label")
        self.forget_all_btn = _button("hosts_forget_all")
        self.forget_all_btn.connect("clicked", self.forget_all)
        self.close_btn = _button("btn_close")
        self.close_btn.connect("clicked", lambda _b: self.close())
        actions.append(self.status)
        actions.append(self.forget_all_btn)
        actions.append(self.close_btn)
        box.append(actions)

        self.set_child(box)
        _bind_escape(self)
        self.load()

    # --------------------------------------------------------------- yukleme
    def load(self, *_a):
        request_async(self.app, "hosts", self._loaded)
        return False

    def _loaded(self, res):
        try:
            child = self.listbox.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                self.listbox.remove(child)
                child = nxt
            if not res.get("ok"):
                self._say(str(res.get("error") or ""))
                return False
            rows = res.get("hosts") or []
            for h in rows:
                self.listbox.append(self._row(h))
            if not rows and not self.status.get_text():
                self._say(gtr("hosts_empty"))   # islem sonucu mesajini ezme
            self.forget_all_btn.set_sensitive(bool(rows))
        except Exception:
            pass
        return False

    def _row(self, h: dict) -> Gtk.ListBoxRow:
        host = str(h.get("host") or "")
        row = Gtk.ListBoxRow()
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        line.set_margin_top(4); line.set_margin_bottom(4)
        line.set_margin_start(6); line.set_margin_end(6)
        text = Gtk.Label(label=present.host_line(h, lang()), xalign=0,
                         hexpand=True, ellipsize=2, selectable=True)
        tip = gtr("hosts_forget")
        forget = _icon_button("edit-delete", tip + ": " + host)
        forget.connect("clicked", self._forget, host)
        line.append(text); line.append(forget)
        row.set_child(line)
        return row

    # --------------------------------------------------------------- eylemler
    def _say(self, text: str) -> None:
        try:
            self.status.set_text(text)
        except Exception:
            pass

    def _forget(self, _btn, host: str):
        request_async(self.app, "hosts", self._forgotten, forget=host)

    def _forgotten(self, res):
        try:
            if not res.get("ok"):
                self._say(gtr("hosts_unknown", host=res.get("host")
                              or res.get("error") or ""))
            else:
                self._say(gtr("hosts_forgotten",
                              host=res.get("forgotten") or ""))
            self.load()
        except Exception:
            pass
        return False

    def forget_all(self, _btn=None):
        self.app.confirm(gtr("hosts_confirm_clear"),
                         lambda: request_async(self.app, "hosts",
                                               self._cleared, clear=True),
                         gtr("hosts_forget_all"))

    def _cleared(self, res):
        try:
            if res.get("ok"):
                self._say(gtr("hosts_cleared", n=res.get("removed") or 0))
            else:
                self._say(str(res.get("error") or ""))
            self.load()
        except Exception:
            pass
        return False
