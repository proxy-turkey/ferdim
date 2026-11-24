"""Ogrenilen ana makine profilleri: baglanti tavani, RTT bandi, baglanti basi hiz.

Biten (ya da anlamli aktarimdan sonra duraklatilan) her indirme olctugu
degerleri ana makine basina bildirir; yeni isler soguk baslangic yerine
ogrenilmis baglanti sayisindan baslar. Profil yalniz BASLANGIC noktasidir:
governor kesif yonunu degistirmez, azami guc varsayilani korunur.

Iki ayri ogrenilmis sayi vardir: `links` aralik (HTTP Range) islerinin
baglanti sayisi, `seg_workers` HLS/DASH segment havuzunun isci sayisidir.
Ayni ana makinede ikisi de bulunabilir; eski hosts.json kayitlari bu alan
yokken de yuklenir (eksik alan 0 sayilir). FTP tek akis oldugu icin
yalnizca RTT bandi ve baglanti basi hiz bildirir.

Dosya durum dizininde 0600 modunda, atomik yazilir (tmp + os.replace);
en fazla MAX_HOSTS kayit tutulur (en eski last_seen duser) ve
MAX_AGE_DAYS gun gormeyen kayitlar atilir.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import util

FILE_NAME = "hosts.json"
MAX_HOSTS = 500           # LRU siniri (last_seen'e gore)
MAX_AGE_DAYS = 90         # gun: bu kadar gormeyen ana makine unutulur
RTT_LOW = 0.05            # sn: altinda "low"
RTT_MID = 0.20            # sn: altinda "mid", ustu "high"
FORMAT_VERSION = 1

# semaya gore varsayilan portlar: URL'de yazilsa da anahtar sadelesir
_DEFAULT_PORTS = {"http": (80,), "https": (443,), "ftp": (21,),
                  "ftps": (21,), "": (80, 443, 21)}

log = logging.getLogger("ferdim.profiles")


def host_key(text: str) -> str:
    """Profil anahtari: kucuk harfli ana makine; varsayilan disi port eklenir.

    Ciplak ana makine de kabul edilir ("ornek.com:8080"): CLI'den unutma
    komutu URL yazmayi gerektirmez; listede gorunen anahtar birebir eslesir.
    Sayisal IP'ler oldugu gibi kalir.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        host = raw.lower()
        # ciplak girdi bir ana makinedir; yol/sorgu/kimlik bilgisi olamaz
        return "" if any(c in host for c in "/?#@ ") else host
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return ""
    if not host:
        return ""
    if ":" in host:                 # IPv6
        host = f"[{host}]"
    if port and port not in _DEFAULT_PORTS.get(parts.scheme, _DEFAULT_PORTS[""]):
        host = f"{host}:{port}"
    return host


def rtt_band(secs: float) -> str:
    """Yanit suresi bandi: low / mid / high (bilinmiyorsa bos)."""
    try:
        secs = float(secs)
    except (TypeError, ValueError):
        return ""
    if secs <= 0:
        return ""
    if secs < RTT_LOW:
        return "low"
    return "mid" if secs < RTT_MID else "high"


def _entry(raw) -> dict | None:
    """Diskten okunan tek kaydi dogrular; bozuksa None."""
    if not isinstance(raw, dict):
        return None
    try:
        return {
            "links": max(0, int(raw.get("links") or 0)),
            "seg_workers": max(0, int(raw.get("seg_workers") or 0)),
            "throttled": bool(raw.get("throttled")),
            "rtt_band": str(raw.get("rtt_band") or ""),
            "per_link_bps": max(0, int(raw.get("per_link_bps") or 0)),
            "samples": max(0, int(raw.get("samples") or 0)),
            "last_seen": float(raw.get("last_seen") or 0),
        }
    except (TypeError, ValueError):
        return None


class HostProfiles:
    """Ana makine basina ogrenilmis profil deposu (JSON + kilit).

    Tum okuma/yazma tek kilit altinda: daemon'daki isler ve CLI ayni
    dosyaya ayni anda dokunabilir; her degisiklik atomik yazilir.
    now_fn testlerde saati ilerletmek icin verilebilir (uyku yok).
    """

    def __init__(self, env, *, path=None, max_hosts: int = MAX_HOSTS,
                 max_age_days: float = MAX_AGE_DAYS, now_fn=time.time):
        self.path = Path(path) if path is not None \
            else Path(env.state_dir) / FILE_NAME
        self.max_hosts = max(1, int(max_hosts))
        self.max_age = max(1.0, float(max_age_days) * 86400)
        self._now = now_fn
        self._lock = threading.Lock()
        self._data: dict[str, dict] | None = None

    # ------------------------------------------------------------- ic yardim
    def _prune_locked(self, now: float) -> None:
        """Suresi dolanlari at, sonra en eski last_seen'leri buda."""
        assert self._data is not None
        cutoff = now - self.max_age
        for host in [h for h, e in self._data.items()
                     if e["last_seen"] < cutoff]:
            del self._data[host]
        extra = len(self._data) - self.max_hosts
        if extra > 0:
            oldest = sorted(self._data, key=lambda h: self._data[h]["last_seen"])
            for host in oldest[:extra]:
                del self._data[host]

    def _load_locked(self, now: float) -> dict[str, dict]:
        """Dosyayi bir kez okur; bozuk dosya bos depo sayilir.

        Prune her okumada calisir: suresi dolan ana makine, araya bir
        record() girmeden de get()/hosts() sonucunda GORUNMEZ.
        """
        if self._data is None:
            raw = util.read_json(self.path)
            hosts = raw.get("hosts") if isinstance(raw, dict) else None
            data: dict[str, dict] = {}
            if isinstance(hosts, dict):
                for host, val in hosts.items():
                    rec = _entry(val)
                    if isinstance(host, str) and host and rec is not None:
                        data[host.lower()] = rec
            self._data = data
        self._prune_locked(now)
        return self._data

    def _save_locked(self) -> None:
        assert self._data is not None
        util.atomic_write_json(self.path,
                               {"version": FORMAT_VERSION, "hosts": self._data})

    # ------------------------------------------------------------------ okuma
    def get(self, text: str) -> dict | None:
        """Tek ana makine profili (kopya) ya da None."""
        key = host_key(text)
        if not key:
            return None
        with self._lock:
            e = self._load_locked(self._now()).get(key)
            return dict(e, host=key) if e else None

    def hosts(self) -> list[dict]:
        """Tum profiller: en son gorulen once."""
        with self._lock:
            rows = [dict(e, host=h)
                    for h, e in self._load_locked(self._now()).items()]
        rows.sort(key=lambda r: (-r["last_seen"], r["host"]))
        return rows

    # ----------------------------------------------------------------- yazim
    def record(self, text: str, *, links: int = 0, seg_workers: int = 0,
               throttled: bool = False, rtt: float = 0.0,
               per_link_bps: float = 0.0, now: float | None = None) -> bool:
        """Bir isin olcumunu isler: links/seg_workers/hiz yumusak ortalamayla
        birlesir.

        links ve seg_workers birbirinden bagimsizdir: bir is yalnizca kendi
        olctugu alani bildirir (FTP ikisini de bildirmez). Disk hatasi
        indirmeyi etkilemez (yalniz loglanir, False doner).
        """
        key = host_key(text)
        if not key:
            return False
        try:
            links = max(0, int(links or 0))
            seg_workers = max(0, int(seg_workers or 0))
            per_link = max(0.0, float(per_link_bps or 0.0))
        except (TypeError, ValueError):
            return False
        try:
            with self._lock:
                stamp = float(self._now() if now is None else now)
                data = self._load_locked(stamp)
                e = data.get(key) or {"links": 0, "seg_workers": 0,
                                      "throttled": False, "rtt_band": "",
                                      "per_link_bps": 0, "samples": 0,
                                      "last_seen": stamp}
                e["samples"] = int(e["samples"] or 0) + 1
                if links > 0:
                    # yumusak ortalama: tek kotu kosu profili bozmaz
                    e["links"] = links if not e["links"] \
                        else max(1, int(round((e["links"] + links) / 2)))
                if seg_workers > 0:
                    e["seg_workers"] = seg_workers if not e["seg_workers"] \
                        else max(1, int(round((e["seg_workers"] + seg_workers) / 2)))
                if throttled:
                    e["throttled"] = True
                band = rtt_band(rtt)
                if band:
                    e["rtt_band"] = band
                if per_link > 0:
                    e["per_link_bps"] = int(per_link) if not e["per_link_bps"] \
                        else int(round((e["per_link_bps"] + per_link) / 2))
                e["last_seen"] = stamp
                data[key] = e
                self._prune_locked(stamp)
                self._save_locked()
            return True
        except (OSError, TypeError, ValueError) as exc:
            log.warning("profil yazilamadi: %s", exc)
            return False

    def forget(self, text: str) -> bool:
        """Tek ana makine profilini siler; kayit yoksa False."""
        key = host_key(text)
        if not key:
            return False
        try:
            with self._lock:
                data = self._load_locked(self._now())
                if data.pop(key, None) is None:
                    return False
                self._save_locked()
            return True
        except (OSError, TypeError, ValueError) as exc:
            log.warning("profil silinemedi: %s", exc)
            return False

    def clear(self) -> int:
        """Tum profilleri siler; silinen kayit sayisini dondurur."""
        try:
            with self._lock:
                data = self._load_locked(self._now())
                removed = len(data)
                data.clear()
                self._save_locked()
            return removed
        except (OSError, TypeError, ValueError) as exc:
            log.warning("profiller silinemedi: %s", exc)
            return 0


_OPEN_LOCK = threading.Lock()


def open_profiles(env) -> HostProfiles:
    """AppEnv basina tek depo: ayni daemon'daki isler ayni profilleri gorur."""
    store = getattr(env, "host_profiles", None)
    if store is not None:
        return store
    with _OPEN_LOCK:
        store = getattr(env, "host_profiles", None)
        if store is None:
            store = HostProfiles(env)
            env.host_profiles = store
    return store
