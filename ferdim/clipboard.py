"""Opsiyonel pano izleyici: dosya benzeri URL kopyalaninca kuyruga ekler.

wl-paste (Wayland)
veya xclip (X11) varsa calisir.

`clipboard_confirm` (varsayilan acik): URL kuyruga DURDURULMUS girer,
eylemli bir masaustu bildirimi gosterilir; kisa bir bekleme penceresinden
(GRACE sn) sonra kendiliginden baslar, "Geri al" secilirse is silinir.
Bildirim tasiyicisi eylem desteklemiyorsa eski davranis korunur: is
hemen kuyruga girer ve yine "panodan kuyruklandi" bildirimi gosterilir.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time

from .i18n import detect_lang, t
from .util import filename_from_url

GRACE = 10.0          # sn: "Geri al" icin taninan pencere
PAUSED = "paused"     # JobState.PAUSED ile ayni (dongusel import yok)


def _start_timer(delay: float, fn):
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()
    return timer


class ClipboardWatcher:
    def __init__(self, qm, cfg, *, notify_action=None, notify_plain=None,
                 run=None, bus_available=None, spawn=None, timer=None,
                 grace=None):
        self.qm = qm
        self.cfg = cfg
        self._stop = threading.Event()
        self._last = ""
        self._action = notify_action      # (baslik, govde, etiket, cb) -> bool
        self._plain = notify_plain        # (baslik, govde) -> bool
        self._run = run                   # testler icin subprocess.run yerine
        self._bus = bus_available
        self._spawn = spawn
        self._timer = timer or _start_timer
        self._timers: list = []
        self.grace = GRACE if grace is None else float(grace)

    # --------------------------------------------------------------- okuma
    def _read(self) -> str:
        try:
            if shutil.which("wl-paste"):
                r = subprocess.run(["wl-paste", "--no-newline"],
                                   capture_output=True, text=True, timeout=2)
                return r.stdout.strip()
            if shutil.which("xclip"):
                r = subprocess.run(
                    ["xclip", "-o", "-selection", "clipboard"],
                    capture_output=True, text=True, timeout=2)
                return r.stdout.strip()
        except Exception:
            pass
        return ""

    def _interesting(self, url: str) -> bool:
        if not url.startswith(("http://", "https://", "ftp://", "ftps://")):
            return False
        exts = [e.strip().lower() for e in
                (self.cfg.get("clipboard_extensions") or "").split()]
        name = filename_from_url(url).lower()
        if exts and not any(name.endswith(e) for e in exts):
            return False
        return url != self._last

    # ------------------------------------------------------------ kuyruklama
    def _notify_action(self, title: str, body: str, label: str, cb) -> bool:
        if self._action is not None:
            return bool(self._action(title, body, label, cb))
        from . import notify
        return bool(notify.notify_action(
            title, body, label, cb,
            timeout_ms=int(max(1.0, self.grace) * 1000),
            run=self._run, bus_available=self._bus, spawn=self._spawn))

    def _notify_plain(self, title: str, body: str) -> bool:
        if self._plain is not None:
            return bool(self._plain(title, body))
        from . import notify
        return bool(notify.send_plain(title, body, run=self._run,
                                      bus_available=self._bus))

    def _schedule(self, delay: float, fn):
        holder: dict = {}

        def fire():
            try:
                fn()
            finally:
                try:
                    self._timers.remove(holder["t"])
                except (KeyError, ValueError):
                    pass

        timer = self._timer(delay, fire)
        holder["t"] = timer
        self._timers.append(timer)
        return timer

    def _resume(self, job_id: str) -> None:
        job = self.qm.get(job_id)
        if job is not None and job.state == PAUSED:
            self.qm.control(job_id, "resume")

    def undo(self, job_id: str) -> bool:
        """Geri al: yalniz hala durdurulmus isi siler (baslamis ise dokunma)."""
        job = self.qm.get(job_id)
        if job is None or job.state != PAUSED:
            return False
        self.qm.remove(job_id)
        return True

    def _queued_notice(self, lang: str, name: str) -> None:
        self._notify_plain(t("clipboard_queued_title", lang),
                           t("clipboard_queued_body", lang, name=name))

    def _queue(self, url: str) -> None:
        """URL'yi kuyruga alir; onay penceresi varsa once durdurur."""
        from .engine.job import JobSpec
        lang = detect_lang(self.cfg.get("language"))
        name = filename_from_url(url)
        if not bool(self.cfg.get("clipboard_confirm", True)):
            self.qm.add(JobSpec(url=url))
            self._queued_notice(lang, name)
            return
        job = self.qm.add(JobSpec(url=url), paused=True)
        title = t("clipboard_confirm_title", lang)
        body = t("clipboard_confirm_body", lang, name=name,
                 secs=int(self.grace))
        try:
            accepted = self._notify_action(
                title, body, t("clipboard_undo", lang),
                lambda: self.undo(job.id))
        except Exception:
            accepted = False
        if accepted:
            self._schedule(self.grace, lambda: self._resume(job.id))
            return
        # Eylem desteklenmiyor: bugunku davranis (hemen kuyruk) + bildirim.
        self.qm.control(job.id, "resume")
        self._queued_notice(lang, name)

    # ----------------------------------------------------------------- dongu
    def _loop(self):
        while not self._stop.is_set():
            text = self._read()
            if text and len(text) < 2048 and self._interesting(text):
                self._last = text
                self._queue(text)
            time.sleep(1.0)

    def start(self):
        # Once olay tabanli dene (Wayland): wl-paste --watch cat
        try:
            proc = subprocess.Popen(
                ["wl-paste", "--watch", "cat"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, start_new_session=True)
            threading.Thread(target=self._watch_loop, args=(proc,),
                             daemon=True, name="ferdim-clipboard-watch").start()
            return
        except Exception:
            pass
        threading.Thread(target=self._loop, daemon=True,
                         name="ferdim-clipboard").start()

    def _watch_loop(self, proc):
        for line in proc.stdout:
            line = line.strip()
            if line and len(line) < 2048 and self._interesting(line):
                self._last = line
                self._queue(line)
        proc.wait()

    def stop(self):
        self._stop.set()
        for timer in self._timers:
            try:
                timer.cancel()
            except Exception:
                pass
        self._timers.clear()
