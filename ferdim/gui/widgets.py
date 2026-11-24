"""GTK yardimcilari, dil durumu ve arka plan istekleri.

app.py ve pencere modulleri bu dosyayi paylasir: dil tek yerde tutulur
(gtr), simgeli dugmelere erisilebilir ad, giris alanlarina animsatici
(mnemonic) etiket verilir. Agir istekler (arsiv listesi, gecmis aramasi)
ana donguyu kilitlemez: her istek KENDI kisa omurlu baglantisinda bir is
parcaciginda kosar, sonuc GLib.idle_add ile ana is parcacigina doner.
Ortak app.client yalniz ana is parcaciginda kullanilir; boylece JSON
satir kanali iki is parcacigi arasinda karismaz.
"""
from __future__ import annotations

import threading

try:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk
except (ImportError, ValueError) as e:  # pragma: no cover
    print("HATA: GTK4 arayuzu icin 'python3-gobject' paketi gerekli.")
    print("  sudo dnf install python3-gobject")
    print("Ayrinti:", e)
    raise SystemExit(1)

from ..client import FerdimClient
from ..i18n import detect_lang, t

STATES = ("queued", "connecting", "downloading", "paused",
          "done", "error", "canceled")

_LANG = "tr"


def set_gui_lang(cfg_lang: str | None) -> None:
    global _LANG
    _LANG = detect_lang(cfg_lang)


def lang() -> str:
    return _LANG


def gtr(key: str, **kw) -> str:
    """Etkin arayuz dilinde ceviri (t() ile ayni, dil gizli)."""
    return t(key, _LANG, **kw)


def tr_state(state: str) -> str:
    return gtr("st_" + state) if state in STATES else state


def _a11y(widget, name: str, values: list) -> None:
    """Erisilebilirlik ozelligi (Gtk.AccessibleProperty): yalniz simgeli
    dugmenin adi, giris alaninin ipucu. GTK surumunde yoksa atlanir."""
    try:
        widget.update_property([getattr(Gtk.AccessibleProperty, name)], values)
    except (AttributeError, TypeError):
        pass


def _under(txt: str) -> str:
    """Cevirideki '_' harfi animsatici isaret olarak kacirilir."""
    return "_" + txt.replace("_", "__")


def _mnemonic(key: str, widget, target=None) -> Gtk.Label:
    """Animsatici etiket: Alt+harf alana gecer; GTK etiket <-> alan
    iliskisini (LABELLED_BY / LABEL_FOR) ekran okuyuculara da verir."""
    lab = Gtk.Label(xalign=0, use_underline=True,
                    mnemonic_widget=target or widget)
    lab.set_label(_under(gtr(key)))
    return lab


def _field(parent, widget, key: str, target=None) -> None:
    parent.append(_mnemonic(key, widget, target))
    parent.append(widget)


def _grid_field(grid, row: int, widget, key: str, target=None) -> None:
    widget.set_hexpand(True)
    grid.attach(_mnemonic(key, widget, target), 0, row, 1, 1)
    grid.attach(widget, 1, row, 1, 1)


def _button(key: str, suggested: bool = False) -> Gtk.Button:
    b = Gtk.Button(use_underline=True)
    b.set_label(_under(gtr(key)))
    if suggested:
        b.get_style_context().add_class("suggested-action")
    return b


def _icon_button(icon: str, tip: str) -> Gtk.Button:
    """Yalniz simgeli dugme: ipucu metni erisilebilir ad olarak da verilir."""
    b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
    b.get_style_context().add_class("flat")
    _a11y(b, "LABEL", [tip])
    return b


def _check(key: str, active: bool) -> Gtk.CheckButton:
    c = Gtk.CheckButton(use_underline=True, active=active)
    c.set_label(_under(gtr(key)))
    return c


def _time_entry(value: str, a11y_name: str | None = None) -> Gtk.Entry:
    e = Gtk.Entry(text=value or "", width_chars=5, max_length=5,
                  placeholder_text="HH:MM")
    if a11y_name:
        _a11y(e, "LABEL", [a11y_name])
    return e


def _bind_escape(win: Gtk.Window) -> None:
    """Escape pencereyi kapatir (eski dialog penceresinin davranisi)."""
    def _close(*_a):
        win.close()
        return True
    ctrl = Gtk.ShortcutController()
    ctrl.add_shortcut(Gtk.Shortcut.new(
        Gtk.ShortcutTrigger.parse_string("Escape"),
        Gtk.CallbackAction.new(_close)))
    win.add_controller(ctrl)


def scrolled(child, height: int = 0, vexpand: bool = True) -> Gtk.ScrolledWindow:
    sw = Gtk.ScrolledWindow(vexpand=vexpand, hexpand=True,
                            hscrollbar_policy=Gtk.PolicyType.NEVER,
                            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
    if height:
        sw.set_size_request(-1, height)
    sw.set_child(child)
    return sw


# --------------------------------------------------------------- arka plan
def run_async(app, work, done) -> None:
    """work(client) ayri bir baglantida kosar; sonuc done(res) ile ana is
    parcacigina doner. done() False dondurmeli (GLib tek atimlik zamanki)."""
    def _worker():
        try:
            client = FerdimClient(str(app.env.socket_path)).connect()
            try:
                res = work(client)
            finally:
                client.close()
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        GLib.idle_add(done, res)
    threading.Thread(target=_worker, daemon=True,
                     name="ferdim-gui-rpc").start()


def request_async(app, cmd: str, done, **params) -> None:
    run_async(app, lambda c: c.request(cmd, **params), done)
