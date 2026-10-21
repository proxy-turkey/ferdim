"""Opsiyonel pano izleyici: dosya benzeri URL kopyalaninca kuyruga ekler.

wl-paste (Wayland)
veya xclip (X11) varsa calisir.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path

from .util import filename_from_url


class ClipboardWatcher:
    def __init__(self, qm, cfg):
        self.qm = qm
        self.cfg = cfg
        self._stop = threading.Event()
        self._last = ""

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
        if not url.startswith(("http://", "https://")):
            return False
        exts = [e.strip().lower() for e in
                (self.cfg.get("clipboard_extensions") or "").split()]
        name = filename_from_url(url).lower()
        if exts and not any(name.endswith(e) for e in exts):
            return False
        return url != self._last

    def _loop(self):
        while not self._stop.is_set():
            text = self._read()
            if text and len(text) < 2048 and self._interesting(text):
                self._last = text
                from .engine.job import JobSpec
                self.qm.add(JobSpec(url=text))
            time.sleep(1.0)

    def start(self):
        threading.Thread(target=self._loop, daemon=True,
                         name="ferdim-clipboard").start()

    def stop(self):
        self._stop.set()
