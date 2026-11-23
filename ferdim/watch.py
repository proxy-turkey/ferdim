"""Izlenen klasorler: icine birakilan baglanti listelerini ve metalink
dosyalarini otomatik kuyruga ekler.

- .metalink / .meta4 : RFC 5854 belgesi (aynalar + saglamalar ile)
- .txt .urls .url .list .links : her satirda bir URL (yorumlar # ; //)

Kuyruga alinan dosya silinir (varsayilan: watch_done=delete), '.done'
eki ile yeniden adlandirilir ya da yerinde birakilir. Yeni yazilmis
(daha 1 sn olmamis) dosyalar okunmaz; ayristirilamayanlar yerinde
birakilir ve oturum boyunca yeniden denenmez.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from . import util

TEXT_EXT = (".txt", ".urls", ".url", ".list", ".links")
METALINK_EXT = (".metalink", ".meta4")
MAX_FILE = 4 << 20
STABLE_SECS = 1.0
STATE_FILE = "watch.json"
ACTIONS = ("delete", "rename", "keep")
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)


def _trim_url(u: str) -> str:
    """Satir sonundaki noktalama ve dengesiz kapanis parantezini atar
    ('...zip)', '...iso,'); URL'nin kendi parantezleri korunur."""
    while True:
        v = u.rstrip(".,;")
        if v.endswith(")") and v.count(")") > v.count("("):
            v = v[:-1]
        if v == u:
            return u
        u = v


def parse_url_list(text: str, limit: int = 500) -> list[str]:
    """Her satirda bir URL; yorum satirlari (# ; //) atlanir, yinelenenler
    cikarilir, sir korunur."""
    out, seen = [], set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "//")):
            continue
        m = URL_RE.search(line)
        if not m:
            continue
        u = _trim_url(m.group(0))
        if u not in seen:
            seen.add(u)
            out.append(u)
            if len(out) >= limit:
                break
    return out


def kind_of(path: Path) -> str:
    """'metalink' | 'list' | '' (ilginc bir dosya degil)."""
    ext = path.suffix.lower()
    if ext in METALINK_EXT:
        return "metalink"
    if ext in TEXT_EXT:
        return "list"
    return ""


class FolderWatcher:
    """cfg['watch_dirs'] icindeki klasorleri cfg['watch_interval'] sn'de
    bir tarar; uygun dosyalari `enqueue(path)` ile kuyruga verir."""

    def __init__(self, qm, cfg, env, enqueue):
        self.qm = qm
        self.cfg = cfg
        self.env = env
        self._enqueue = enqueue            # (path) -> {"ok": bool, ...}
        self._path = env.state_dir / STATE_FILE
        self._done: set[str] = set()       # islenen imzalar (yerinde kalanlar)
        self._failed: set[str] = set()     # bu oturumda ayristirilamayanlar
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load()

    # ------------------------------------------------------------- tarama
    def _dirs(self) -> list[Path]:
        out = []
        dirs = self.cfg.get("watch_dirs")
        for d in (dirs if isinstance(dirs, list) else []):
            try:
                p = Path(str(d)).expanduser()
                if p.is_dir() and p not in out:
                    out.append(p)
            except (OSError, ValueError):
                continue
        return out

    def poll(self) -> int:
        """Klasorleri tara; kuyruga eklenen dosya sayisi. Senkron (testler
        dogrudan cagirir), calisma zamaninda donguden gelir."""
        n = 0
        now = time.time()
        for d in self._dirs():
            try:
                entries = sorted(d.iterdir())
            except OSError:
                continue
            for p in entries:
                try:
                    if p.name.startswith(".") or not kind_of(p):
                        continue
                    st = p.stat()
                    if not p.is_file() or st.st_size > MAX_FILE:
                        continue
                    if now - st.st_mtime < STABLE_SECS:
                        continue        # hala yaziliyor olabilir
                    sig = f"{p}:{st.st_size}:{st.st_mtime_ns}"
                    if sig in self._done or sig in self._failed:
                        continue
                    r = self._enqueue(str(p))
                    if not (isinstance(r, dict) and r.get("ok")):
                        self._failed.add(sig)
                        continue
                except OSError:
                    continue
                self._consume(p, sig)
                n += 1
        return n

    def _consume(self, p: Path, sig: str) -> None:
        action = str(self.cfg.get("watch_done", "delete"))
        try:
            if action == "delete":
                p.unlink()
            elif action == "rename":
                p.rename(p.with_name(p.name + ".done"))
        except OSError:
            pass        # silinemedi: imza kaydi tekrar eklenmesini onler
        self._done.add(sig)
        self._save()

    # ---------------------------------------------------------- kalicilik
    def _load(self):
        data = util.read_json(self._path)
        if isinstance(data, list):
            self._done = {str(x) for x in data[-5000:]}

    def _save(self):
        try:
            util.atomic_write_json(self._path, sorted(self._done)[-5000:])
        except OSError:
            pass

    # -------------------------------------------------------------- dongu
    def _interval(self) -> float:
        try:
            return max(1.0, min(60.0, float(self.cfg.get("watch_interval", 3))))
        except (TypeError, ValueError):
            return 3.0

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:
                pass
            self._stop.wait(self._interval())

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ferdim-watch")
        self._thread.start()

    def stop(self):
        self._stop.set()
