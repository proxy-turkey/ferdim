"""Masaustu bildirimleri: portal / freedesktop / notify-send.

Kuyruk dinleyicisi olarak calisir; isler 'done' ya da 'error' durumuna
ulasinca (saglama sonucuyla birlikte) arka planda bildirim gonderir.
Bildirim gonderimi kuyrugu asla bloklamaz: olaylar kuyruga yazilir,
tek bir isci ipligi bunlari birlestirip gonderir. Hicbir tasiyici yoksa
(oturum veriyolu yok, komutlar kurulu degil) sessizce no-op olur; yalnizca
gunluge bir kez bilgi satiri yazilir.
"""
from __future__ import annotations

import logging
import os
import stat
import subprocess
import threading
import time

from . import util
from .i18n import detect_lang, error_text, t

APP_NAME = "FerDim"
log = logging.getLogger("ferdim.notify")

# Portal -> freedesktop -> notify-send. Portal bir masaustu kabugu
# tarafindan sunuluyorsa (sandbox dahil) tercih edilir.
_BACKENDS = ("portal", "freedesktop", "notify-send")


def _bus_address() -> str:
    """Oturum veriyolu adresi; env yoksa XDG_RUNTIME_DIR/bus soketi."""
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if addr:
        return addr
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        path = os.path.join(runtime, "bus")
        try:
            if stat.S_ISSOCK(os.stat(path).st_mode):
                return "unix:path=" + path
        except OSError:
            pass
    return ""


def _bus_available() -> bool:
    return bool(_bus_address())


def _child_env() -> dict | None:
    """Alt komutlar icin ortam; DBUS adresi yoksa ve bulunamadiysa None."""
    addr = _bus_address()
    if not addr:
        return None
    env = dict(os.environ)
    env["DBUS_SESSION_BUS_ADDRESS"] = addr
    return env


def _run(cmd: list[str]) -> bool:
    """Komutu calistirir; basarili (rc=0) mi? Hata/eksik komut = False."""
    env = _child_env()
    if env is None:
        return False
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=6.0, env=env)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _gv(text: str) -> str:
    """GVariant metin literali: tek tirnak ve ters bolu kacirilir."""
    out = (text.replace("\\", "\\\\").replace("'", "\\'")
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return "'" + out + "'"


def _portal(run, title: str, body: str) -> bool:
    nid = "ferdim-%d-%d" % (os.getpid(), int(time.time() * 1000))
    payload = ("{'title': <" + _gv(title) + ">, 'body': <" + _gv(body) +
               ">, 'priority': <'normal'>}")
    return run([
        "gdbus", "call", "--session",
        "--dest", "org.freedesktop.portal.Desktop",
        "--object-path", "/org/freedesktop/portal/desktop",
        "--method", "org.freedesktop.portal.Notification.AddNotification",
        nid, payload,
    ])


def _freedesktop(run, title: str, body: str) -> bool:
    return run([
        "gdbus", "call", "--session",
        "--dest", "org.freedesktop.Notifications",
        "--object-path", "/org/freedesktop/Notifications",
        "--method", "org.freedesktop.Notifications.Notify",
        _gv(APP_NAME), "uint32 0", _gv("ferdim"), _gv(title), _gv(body),
        "@as []", "@a{sv} {}", "int32 5000",
    ])


def _notify_send(run, title: str, body: str) -> bool:
    return run(["notify-send", "--app-name=" + APP_NAME, "--icon=ferdim",
                title, body])


def _compose(events: list[dict], lang: str) -> tuple[str, str]:
    """Olay listesini tek bir baslik/govdeye cevirir; patlama = ozet."""
    if len(events) > 1:
        failed = sum(1 for e in events if e["kind"] == "error")
        done = len(events) - failed
        title = t("notify_summary_title", lang, n=len(events))
        body = t("notify_summary_body", lang, done=done, failed=failed)
        warns = sum(1 for e in events if e.get("checksum") == "warn")
        if warns:
            body += " " + t("notify_summary_warn", lang, n=warns)
        return title, body

    ev = events[0]
    name = ev.get("name") or ev.get("id") or ""
    size = ev.get("size")
    label = name
    if isinstance(size, int) and size > 0:
        label = "%s (%s)" % (name, util.human_size(size))
    if ev["kind"] == "error":
        reason = error_text(ev.get("error_code") or "", ev.get("error") or "",
                            lang)
        return t("notify_error_title", lang), \
            ("%s: %s" % (label, reason) if reason else label)

    chk = ev.get("checksum") or ""
    note = {"ok": t("notify_chk_ok", lang),
            "warn": t("notify_chk_warn", lang),
            "mismatch": t("notify_chk_mismatch", lang)}.get(chk, "")
    title = t("notify_verify_title" if ev["kind"] == "verify"
              else "notify_done_title", lang)
    return title, ("%s — %s" % (label, note) if note else label)


class Notifier:
    """QueueManager dinleyicisi + birlesik bildirim iscisi."""

    def __init__(self, cfg: dict, *, run=None, bus_available=None,
                 coalesce: float = 0.8, min_interval: float = 4.0,
                 clock=time.monotonic):
        self.cfg = cfg
        self._run = run or _run
        self._bus = bus_available or _bus_available
        self._coalesce = coalesce
        self._min_interval = min_interval
        self._clock = clock
        self._cv = threading.Condition()
        self._events: list[dict] = []
        self._seen: dict[str, tuple[str, str]] = {}
        self._seen_lock = threading.Lock()
        self._last_sent: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._qm = None
        self._no_bus_logged = False
        self._no_backend_logged = False

    # -------------------------------------------------------------- yasam
    def start(self, qm=None) -> "Notifier":
        if qm is not None and self._qm is None:
            self._qm = qm
            qm.add_listener(self.on_job)
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop,
                                            name="ferdim-notify", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._qm is not None:
            self._qm.remove_listener(self.on_job)
            self._qm = None
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._thread = None

    # ------------------------------------------------------------ dinleyici
    def on_job(self, snap) -> None:
        """QueueManager.add_listener cagrisi: is durum gecislerini izler."""
        if not isinstance(snap, dict):
            return
        jid = snap.get("id") or ""
        if not jid:
            return
        state = str(snap.get("state") or "")
        chk = str(snap.get("checksum") or "")
        with self._seen_lock:
            prev = self._seen.get(jid)
            self._seen[jid] = (state, chk)
            if state == "canceled":
                self._seen.pop(jid, None)
        if state == "canceled":
            return
        # Ilk gorulen durum taban cizgidir: daemon acilirken kuyrukta
        # duran bitmis isler kullaniciya yeniden bildirilmez.
        if prev is None:
            return
        if not self.cfg.get("notify", True):
            return
        errors_only = bool(self.cfg.get("notify_errors_only", False))
        ev = None
        if state != prev[0]:
            if state == "done" and not errors_only:
                ev = self._event(snap, "done")
            elif state == "error":
                ev = self._event(snap, "error")
        elif state == "done" and chk != prev[1] and not errors_only \
                and chk in ("ok", "warn"):
            # Geciken yan dosya dogrulamasi: is zaten 'done' idi
            ev = self._event(snap, "verify")
        if ev is not None:
            with self._cv:
                self._events.append(ev)
                self._cv.notify()

    @staticmethod
    def _event(snap: dict, kind: str) -> dict:
        size = snap.get("size")
        return {
            "kind": kind,
            "id": snap.get("id") or "",
            "name": snap.get("filename") or snap.get("url") or "",
            "size": size if isinstance(size, int) else -1,
            "checksum": snap.get("checksum") or "",
            "error_code": snap.get("error_code") or "",
            "error": snap.get("error") or "",
        }

    # ---------------------------------------------------------------- isci
    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                while not self._events and not self._stop.is_set():
                    self._cv.wait(0.5)
                if self._stop.is_set():
                    return
            # Patlama penceresi: art arda biten isler tek ozete toplanir
            self._stop.wait(self._coalesce)
            if self._last_sent is not None:
                wait = self._min_interval - (self._clock() - self._last_sent)
                if wait > 0:
                    self._stop.wait(wait)
            self._drain()

    def _drain(self) -> bool:
        """Bekleyen olaylari tek bildirim olarak gonderir (test edilebilir)."""
        with self._cv:
            batch, self._events = self._events, []
        if not batch:
            return False
        lang = detect_lang(self.cfg.get("language"))
        title, body = _compose(batch, lang)
        self._last_sent = self._clock()
        self._deliver(title, body)
        return True

    def _deliver(self, title: str, body: str) -> bool:
        if not self._bus():
            if not self._no_bus_logged:
                log.info("masaustu bildirimi yok: DBUS_SESSION_BUS_ADDRESS "
                         "ya da XDG_RUNTIME_DIR/bus bulunamadi")
                self._no_bus_logged = True
            return False
        self._no_bus_logged = False
        for backend in _BACKENDS:
            fn = {"portal": _portal, "freedesktop": _freedesktop,
                  "notify-send": _notify_send}[backend]
            if fn(self._run, title, body):
                self._no_backend_logged = False
                return True
        if not self._no_backend_logged:
            log.info("masaustu bildirimi gonderilemedi: portal/"
                     "org.freedesktop.Notifications/notify-send erisilemedi")
            self._no_backend_logged = True
        return False
