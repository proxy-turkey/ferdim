"""XDG dizinleri, yapilandirma ve calisma zamani dosyalari."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

from . import DEFAULT_USER_AGENT
from . import util

# config.json'a es zamanli kayitlar (daemon + API) birbirini ezmesin
_CONFIG_LOCK = threading.Lock()

DEFAULTS = {
    "download_dir": None,          # None -> xdg-user-dir DOWNLOAD
    "segments": 32,                # azami guc VARSAYILAN (1-32)
    "max_concurrent": 8,
    "speed_limit": 0,              # bayt/sn; 0 = sinirsiz (varsayilan)
    "timeout": 30,
    "retries": 5,
    "user_agent": DEFAULT_USER_AGENT,
    "verify_tls": True,
    "http3": "off",                # HTTP/3: off | auto | force (aioquic eklentisi)
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
    # --- saglamlik / otomasyon (1.4.1) ---
    "stall_timeout": 20,           # sn: bu kadar sure bayt gelmezse akis durdu
    "checksum_sidecar": True,      # <url>.sha256/.md5 yan dosyasini otomatik dogrula
    "watch_dirs": [],              # izlenen klasorler (baglanti listesi / metalink)
    "watch_done": "delete",        # kuyruga alinan dosya: delete | rename | keep
    "watch_interval": 3,           # tarama araligi (sn)
    # HTTP API'nin yazabilecegi ek kokler; Unix soketi kisitli degildir.
    "allowed_dirs": [],
    # --- kuyruk gecmisi (1.5) ---
    "history_days": 365,           # 0 = gecmisi sonsuza kadar sakla
    # --- ogrenilen ana makine profilleri (1.6) ---
    "learn_hosts": True,           # biten isin baglanti tavani/RTT'sini hatirla
    # --- birlikte yasam (2.0) ---
    # Varsayilan KAPALI: azami guc. Acilirsa hat mesgulken (yabanci trafik
    # gozlenen kapasitenin bu oranini gecerse) baglanti sayisi yarilanir.
    "coexist": False,
    "coexist_threshold": 0.15,
    # Kendi baglantilarimizin TTFB sismesi esigi: guncel EMA / taban
    # (1.2-20). Hat doymadan yavaslamayi yakalar.
    "coexist_latency": 2.5,
    # --- saat bazli hiz plani (1.8) ---
    # [{"days": "mon-fri", "from": "09:00", "to": "18:00", "limit": "2MB"}]
    # Ilk eslesen kural kazanir; pencere gece yarisini asabilir; kural
    # eslesmezse azami guc. Elle verilen limit plani ezer ('limit plan').
    "speed_plan": [],
    "speed_plan_manual": False,
    # --- tikaniklik / LAN esbaz onbellegi (2.x) ---
    # Cekirdek TCP_INFO (RTT, yeniden iletim) governor'a EK tavan sinyali
    # verir; kapatmak eski davranisi birebir korur.
    "tcp_info": True,
    # Esbaz onbellegi istege baglidir: acikken beacon duyurulur ve parca
    # sunulur. Beacon'lar hangi Merkle koklerine sahip oldugumuzu LAN'a
    # bildirir; bu yuzden varsayilan kapalidir.
    "peer_cache": False,
    "peer_port": 8379,
    "peer_dirs": [],               # bos = indirme klasoru taranir
}


CONFIG_VERSION = 1          # config.json sema surumu


def _config_migration_1(raw: dict) -> None:
    """v1: ilk surum - 'version' alani; eski dosya duz bir sozluktu."""


# ileri goc: her adim bir onceki surumun uzerine ekler (history.py gibi)
CONFIG_MIGRATIONS = {1: _config_migration_1}


def _migrate_config(raw: dict) -> dict:
    """config.json'u sirali goclardan gecirir; bilinmeyen anahtarlar korunur.

    Surum alani olmayan dosya v0 sayilir. Dosya bizden YENI bir surum
    tasiyorsa (v > CONFIG_VERSION) goc calistirilmaz: bilinen anahtarlar
    okunur, bilinmeyenler oldugu gibi birakilir (ileri uyumluluk).
    """
    raw = dict(raw)
    try:
        version = max(0, int(raw.get("version") or 0))
    except (TypeError, ValueError):
        version = 0
    while version < CONFIG_VERSION:
        CONFIG_MIGRATIONS[version + 1](raw)
        version += 1
        raw["version"] = version
    return raw


def fsync_dir(path: Path) -> None:
    """Dizini fsync eder: os.replace sonrasi ad degisikligi diske iner.

    Dosya icerigi ayrica fsync edilir (atomic_write_json / save_config);
    dizin kaydi de inmezse guc kesintisinde eski ad gorulebilir. Dizine
    fsync desteklemeyen dosya sistemlerinde sessizce gecilir.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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
        try:
            os.chmod(self.config_dir, 0o700)
        except OSError:
            pass
        self.config_path = self.config_dir / "config.json"
        self.socket_path = self.state_dir / "ferdim.sock"
        self.token_path = self.state_dir / "token"
        self.pid_path = self.state_dir / "ferdim.pid"
        # tek ornek kilidi: daemon omru boyunca flock ile tutulur
        self.lock_path = self.state_dir / "ferdim.lock"
        self.port_path = self.state_dir / "port"
        self.jobs_dir = self.state_dir / "jobs"
        self.log_path = self.state_dir / "ferdim.log"
        self.web_dir = Path(__file__).resolve().parent.parent / "web"
        self._fixed_dir = root is not None   # test koku: dizin sabittir
        self._dir_cfg: dict | None = None     # daemon'in canli yapilandirmasi

    # --- indirme dizini -------------------------------------------------
    @property
    def download_dir(self) -> Path:
        if self._download_dir is None:
            d = (self._dir_cfg if self._dir_cfg is not None
                 else self.load_config()).get("download_dir")
            self._download_dir = Path(d).expanduser() if d else default_download_dir()
            self._download_dir.mkdir(parents=True, exist_ok=True)
        return self._download_dir

    def reload_download_dir(self, cfg: dict | None = None) -> None:
        """Indirme dizini yeniden cozulur: onbellegi bosa cikarir. cfg
        verilirse dizin bu canli sozlukten okunur (config dosyasi kaydedilmemis
        olabilir); test kokunde sabit dizin dokunulmaz."""
        if self._fixed_dir:
            return
        if isinstance(cfg, dict):
            self._dir_cfg = cfg
        self._download_dir = None

    # --- config ----------------------------------------------------------
    def load_config(self) -> dict:
        """Varsayilanlar + dosya; dosya surum gocunden gecer.

        Bozuk dosya varsayilanlara duser (kuyruk gibi karantinaya alinmaz:
        config yeniden yazilabilir bir tercih dosyasidir). Dondurulen sozluk
        'version' tasir; bilinmeyen anahtarlar korunur.
        """
        cfg = dict(DEFAULTS)
        if self.config_path.exists():
            try:
                raw = json.loads(self.config_path.read_text("utf-8"))
            except Exception:
                raw = None
            if isinstance(raw, dict):
                cfg.update(_migrate_config(raw))
        cfg.setdefault("version", CONFIG_VERSION)
        return cfg

    def save_config(self, cfg: dict) -> None:
        """Atomik + 0600 yazim: config.json kimlik bilgisi tasiyabilir ve
        umask ne olursa olsun yalniz kullaniciya acik kalmali. Paylasilan
        `util.atomic_write_bytes` kullanilir: benzersiz gecici ad (sabit
        '.tmp' yok), dosya + dizin fsync. Bicim insan tarafindan okunabilir
        kalir (indent=2)."""
        out = dict(cfg)
        out.setdefault("version", CONFIG_VERSION)
        data = json.dumps(out, indent=2, ensure_ascii=False).encode("utf-8")
        with _CONFIG_LOCK:      # surec ici yazarlar sirayla (surecler arasi: benzersiz tmp)
            self.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            util.atomic_write_bytes(self.config_path, data, mode=0o600)
