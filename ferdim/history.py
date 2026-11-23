"""Kuyruk gecmisi: SQLite tabanli kalici is kaydi, arama ve istatistik.

Her biten is (done / error / canceled) tek satir olur; arama, yeniden
indirme ve toplu istatistik bu dosyadan uretilir. Dosya durum dizininde
0600 modunda tutulur, WAL ile yazilir. Hicbir veritabani hatasi cagirana
sizmaz: gecmis, indirme kuyrugunun onune gecemez (yalnizca loglanir).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import util

SCHEMA_VERSION = 1
STATES = ("done", "error", "canceled")      # gecmise yazilan son durumlar
DB_NAME = "history.db"
log = logging.getLogger("ferdim.history")

# API listelerinde asla gorunmeyen ic kolonlar (opts: yeniden indirme
# parametreleri; kimlik bilgisi tasiyabilir, yalniz sunucu icinde kullanilir)
_COLUMNS = ("id, url, filename, host, size, bytes, duration, avg_links, "
            "peak_links, state, error_code, checksum, finished_at")

_DDL = """
CREATE TABLE IF NOT EXISTS history (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    filename    TEXT NOT NULL DEFAULT '',
    host        TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    bytes       INTEGER NOT NULL DEFAULT 0,
    duration    REAL NOT NULL DEFAULT 0,
    avg_links   REAL NOT NULL DEFAULT 0,
    peak_links  INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL,
    error_code  TEXT NOT NULL DEFAULT '',
    checksum    TEXT NOT NULL DEFAULT '',
    finished_at REAL NOT NULL,
    opts        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS history_finished ON history(finished_at);
CREATE INDEX IF NOT EXISTS history_host ON history(host);
"""


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)


# ileri goc: her adim bir onceki surumun uzerine ekler
MIGRATIONS = {1: _migration_1}


def _migrate(conn: sqlite3.Connection) -> int:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    while version < SCHEMA_VERSION:
        MIGRATIONS[version + 1](conn)
        version += 1
        conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    return version


def host_of(url: str) -> str:
    """URL'den kucuk harfli sunucu adi (istatistik gruplamasi icin)."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _url_password(url: str) -> str:
    """URL userinfo'sindeki sifre. Gosterilen URL'den silinir; yeniden
    indirme icin yalniz DB'deki opts alaninda saklanir."""
    try:
        password = urlsplit(url).password
    except ValueError:
        return ""
    return unquote(password) if password else ""


def _options(job, spec) -> str:
    """Yeniden indirme parametreleri (add komutunun alan adlariyla).

    Sifre yalniz burada durur; arama/liste yanitlarina hic girmez.
    """
    try:
        directory = str(job.target_dir)
    except (OSError, AttributeError, ValueError):
        directory = ""
    password = spec.password or _url_password(getattr(job, "url", "") or "")
    opts = {
        "filename": getattr(job, "final_filename", "") or spec.filename,
        "dir": directory,
        "segments": spec.segments,
        "headers": [f"{k}: {v}" for k, v in (spec.headers or {}).items()],
        "cookies": spec.cookies,
        "user": spec.user,
        "password": password,
        "proxy": spec.proxy,
        "insecure": bool(spec.insecure),
        "referer": spec.referer,
        "ua": spec.user_agent,
        "mirrors": list(spec.mirrors or []),
        "checksums": spec.wanted(),
        "limit": int(spec.limit or 0),
    }
    return json.dumps({k: v for k, v in opts.items() if v}, ensure_ascii=False)


def _row(job) -> tuple:
    """Is nesnesinden gecmis satiri. Kilit ALINMAZ: cagri, isin kendi
    kilidi tutulurken de gelebilir; okunan alanlar tek tek atanir."""
    finished = time.time()
    spec = getattr(job, "spec", None)
    url = util.redact_url(getattr(job, "url", "") or "")
    filename = (getattr(job, "final_filename", "") or
                util.filename_from_url(getattr(job, "url", "") or ""))
    created = float(getattr(job, "created", finished) or finished)
    samples = int(getattr(job, "_links_samples", 0) or 0)
    links_sum = int(getattr(job, "_links_sum", 0) or 0)
    size = int(getattr(job, "total", 0) or 0)
    done = max(0, int(getattr(job, "done", 0) or 0))
    if samples:
        avg_links = links_sum / samples
    else:
        # tek tick'ten kisa islerde ornek yok: bayt tasiyan en az bir
        # baglanti vardi (0 baglanti gostermek yanlis olur)
        avg_links = float(getattr(job, "_active", 0) or (1 if done > 0 else 0))
    peak_links = int(getattr(job, "_links_peak", 0) or 0)
    if done > 0:
        peak_links = max(peak_links, 1)
    extra = getattr(job, "extra", None) or {}
    # sure: yalniz AKTIF saniyeler (connecting/downloading). Kuyrukta bekleme
    # ve duraklama sayilmaz; eski hesap (bitis - olusturma) duraklatilan isi
    # oldugundan cok uzun gosteriyordu.
    active = getattr(job, "active_seconds", None)
    duration = float(active()) if callable(active) \
        else max(0.0, finished - created)
    return (
        str(getattr(job, "id", "")),
        url,
        filename,
        host_of(url),
        max(0, size),
        done,
        duration,
        round(avg_links, 2),
        peak_links,
        str(getattr(job, "state", "")),
        str(getattr(job, "error_code", "") or ""),
        str(extra.get("checksum", "") or ""),
        finished,
        _options(job, spec) if spec is not None else "{}",
    )


def _like(text: str) -> str:
    """LIKE deseni: kullanici metni joker karakter icermez."""
    esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


class History:
    """Gecmis veritabani. Islem basina kisa omurlu baglanti acilir; bu,
    kuyruk is parcaciklariyla ayni anda calismayi guvenli kilar."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._ready = False

    # ------------------------------------------------------------ baglanti
    def _ensure_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        try:
            os.chmod(self.path, 0o600)   # eski/gevsek modu duzelt
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        self._ensure_file()
        conn = sqlite3.connect(str(self.path), timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        if not self._ready:
            with self._lock:
                if not self._ready:
                    # WAL kalici baslikta saklanir: yalniz goc aninda ayarla
                    conn.execute("PRAGMA journal_mode=WAL")
                    _migrate(conn)
                    self._ready = True
        return conn

    def _exec(self, fn, default):
        """fn(conn) calistirir; SQLite/OS hatasi loglanip default doner."""
        conn = None
        try:
            conn = self._connect()
            return fn(conn)
        except (sqlite3.Error, OSError) as e:
            log.warning("gecmis veritabani hatasi: %s", e)
            return default
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    # --------------------------------------------------------------- yazim
    def record(self, job) -> bool:
        """Biten isi yazar (varsa ayni kimligi gunceller). Hata yutulur."""
        try:
            row = _row(job)
        except Exception as e:              # bozuk is nesnesi kuyrugu durdurmaz
            log.warning("gecmis kaydi olusturulamadi: %s", e)
            return False
        if not row[0]:
            return False
        return bool(self._exec(lambda c: self._upsert(c, row), False))

    @staticmethod
    def _upsert(conn: sqlite3.Connection, row: tuple) -> bool:
        conn.execute(
            "INSERT OR REPLACE INTO history (id, url, filename, host, size,"
            " bytes, duration, avg_links, peak_links, state, error_code,"
            " checksum, finished_at, opts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        conn.commit()
        return True

    # --------------------------------------------------------------- okuma
    def get(self, row_id: str) -> dict | None:
        """Tek kayit, opts dahil (yalniz sunucu ici: yeniden indirme)."""
        def run(conn):
            row = conn.execute(
                f"SELECT {_COLUMNS}, opts FROM history WHERE id = ?",
                (str(row_id or ""),)).fetchone()
            if row is None:
                return None
            out = dict(row)
            try:
                out["opts"] = json.loads(out.get("opts") or "{}")
            except ValueError:
                out["opts"] = {}
            return out
        return self._exec(run, None)

    def search(self, text: str = "", state: str = "", limit=50,
               offset=0) -> dict | None:
        """Metin (URL/dosya/sunucu) ve durum suzgeciyle arama."""
        try:
            limit = max(1, min(200, int(limit or 50)))
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            limit, offset = 50, 0
        where, args = [], []
        text = (text or "").strip()
        if text:
            like = _like(text)
            where.append("(url LIKE ? ESCAPE '\\' OR filename LIKE ? ESCAPE '\\'"
                         " OR host LIKE ? ESCAPE '\\')")
            args += [like, like, like]
        if state in STATES:
            where.append("state = ?")
            args.append(state)
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        def run(conn):
            total = conn.execute(
                "SELECT COUNT(*) FROM history" + clause, args).fetchone()[0]
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM history{clause}"
                " ORDER BY finished_at DESC LIMIT ? OFFSET ?",
                [*args, limit, offset]).fetchall()
            return {"items": [dict(r) for r in rows], "total": int(total)}
        return self._exec(run, None)

    def stats(self) -> dict | None:
        """Toplam bayt/kayit ve sunucu basina ortalama baglanti/hiz."""
        def run(conn):
            row = conn.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(bytes), 0) AS bytes,"
                " COALESCE(SUM(size), 0) AS size,"
                " COALESCE(SUM(duration), 0) AS duration FROM history").fetchone()
            hosts = conn.execute(
                "SELECT host, COUNT(*) AS count, COALESCE(SUM(bytes), 0) AS bytes,"
                " AVG(avg_links) AS avg_links, AVG(peak_links) AS peak_links,"
                " SUM(bytes) * 1.0 / NULLIF(SUM(duration), 0) AS avg_speed"
                " FROM history WHERE host <> '' GROUP BY host"
                " ORDER BY bytes DESC, host LIMIT 50").fetchall()
            return {
                "count": int(row["count"]),
                "bytes": int(row["bytes"]),
                "size": int(row["size"]),
                "duration": float(row["duration"]),
                "hosts": [dict(h) for h in hosts],
            }
        return self._exec(run, None)

    # --------------------------------------------------------------- silme
    def clear(self, older_than_days=None) -> int:
        """Tum gecmisi ya da yalniz eski kayitlari siler."""
        cutoff = None
        if older_than_days is not None:
            try:
                days = max(0, int(older_than_days))
            except (TypeError, ValueError):
                days = 0
            cutoff = time.time() - days * 86400

        def run(conn):
            if cutoff is None:
                cur = conn.execute("DELETE FROM history")
            else:
                cur = conn.execute(
                    "DELETE FROM history WHERE finished_at < ?", (cutoff,))
            conn.commit()
            return int(cur.rowcount or 0)
        return int(self._exec(run, 0) or 0)

    def prune(self, days) -> int:
        """history_days: 0 (ya da gecersiz) = sonsuza kadar sakla."""
        try:
            days = int(days or 0)
        except (TypeError, ValueError):
            days = 0
        if days <= 0:
            return 0
        return self.clear(older_than_days=days)


def open_history(env) -> History:
    """Durum dizinindeki standart gecmis dosyasi."""
    return History(env.state_dir / DB_NAME)
