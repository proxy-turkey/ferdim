"""XDG dizinleri, yapilandirma ve calisma zamani dosyalari."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import DEFAULT_USER_AGENT

DEFAULTS = {
    "download_dir": None,          # None -> xdg-user-dir DOWNLOAD
    "segments": 32,                # azami guc VARSAYILAN (1-32)
    "max_concurrent": 8,
    "speed_limit": 0,              # bayt/sn; 0 = sinirsiz (varsayilan)
    "timeout": 30,
    "retries": 5,
    "user_agent": DEFAULT_USER_AGENT,
    "verify_tls": True,
    "schedule": {"enabled": False, "start": "02:00", "stop": "08:00"},
    "clipboard_watch": False,
    "clipboard_min_size": 5_000_000,
    "clipboard_extensions": ".mp4 .mkv .webm .flv .avi .mov .m4a .mp3 "
                             ".zip .rar .7z .tar .gz .iso .exe .apk .deb .rpm .pdf",
    "web_port": 8378,
    "web_enabled": True,
    "keep_canceled": True,
    # Azami guc varsayilan davranistir; turbo anahtari yalnizca nazik
    # paylasim icin kapatma/kapatma-acma imkani verir.
    "turbo_mode": True,
    "turbo_segments": 32,
    "turbo_concurrency": 8,
    "language": "auto",
}


def _xdg(env_var: str, default: str) -> Path:
    return Path(os.environ.get(env_var) or Path.home() / default).expanduser()


def default_download_dir() -> Path:
    try:
        out = subprocess.run(
            ["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=5
        )
        p = Path(out.stdout.strip()) if out.stdout.strip() else None
        if p and p.is_dir():
            return p
    except Exception:
        pass
    for cand in (Path.home() / "Downloads", Path.home() / "İndirilenler"):
        if cand.is_dir():
            return cand
    return Path.home() / "Downloads"


class AppEnv:
    """Tum dizinler tek yerden; testler root gecersin diye enjekte edilebilir."""

    def __init__(self, root: Path | None = None):
        if root is not None:  # test modu: her sey tek kok altinda
            root = Path(root)
            self.state_dir = root / "state"
            self.config_dir = root / "config"
            self._download_dir = root / "downloads"
        else:
            self.state_dir = _xdg("XDG_STATE_HOME", ".local/state") / "ferdim"
            self.config_dir = _xdg("XDG_CONFIG_HOME", ".config") / "ferdim"
            self._download_dir = None
        for d in (self.state_dir, self.config_dir, self.state_dir / "jobs"):
            d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass
        self.config_path = self.config_dir / "config.json"
        self.socket_path = self.state_dir / "ferdim.sock"
        self.token_path = self.state_dir / "token"
        self.pid_path = self.state_dir / "ferdim.pid"
        self.port_path = self.state_dir / "port"
        self.jobs_dir = self.state_dir / "jobs"
        self.log_path = self.state_dir / "ferdim.log"
        self.web_dir = Path(__file__).resolve().parent.parent / "web"

    # --- indirme dizini -------------------------------------------------
    @property
    def download_dir(self) -> Path:
        if self._download_dir is None:
            cfg = self.load_config()
            d = cfg.get("download_dir")
            self._download_dir = Path(d).expanduser() if d else default_download_dir()
            self._download_dir.mkdir(parents=True, exist_ok=True)
        return self._download_dir

    # --- config ----------------------------------------------------------
    def load_config(self) -> dict:
        cfg = dict(DEFAULTS)
        if self.config_path.exists():
            try:
                cfg.update(json.loads(self.config_path.read_text("utf-8")))
            except Exception:
                pass
        return cfg

    def save_config(self, cfg: dict) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(self.config_path)
