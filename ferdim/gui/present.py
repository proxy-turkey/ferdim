"""GTK pencereleri icin saf bicimlendirme (Gtk importu yok).

Satir metinleri (gecmis, ana makine profili, saglama rozeti) burada
uretilir: GTK'siz test edilebilir ve web/CLI ile ayni i18n anahtarlarini
kullanir. Simgeler eylem/renk degil durum bildirir; renk CSS sinifindan
gelir (success / warning / error).
"""
from __future__ import annotations

import re
import time

from .. import util
from ..history import STATES as HIST_STATES
from ..i18n import t

# web arayuzuyle ayni: bitmis ve arsiv olabilecek dosyalar listelenebilir
ARCHIVE_RE = re.compile(
    r"\.(zip|tar|tgz|tbz2|txz|tar\.(gz|xz|bz2|zst))$", re.IGNORECASE)

_BADGE = {3: ("✗", "error"), 2: ("⚠", "warning"), 1: ("✓", "success")}


def is_archive(job: dict) -> bool:
    """Arsiv penceresi bu is icin anlamli mi (bitmis + arsiv uzantisi)."""
    return (job.get("state") == "done"
            and bool(ARCHIVE_RE.search(job.get("filename") or "")))


def integrity_badge(job: dict, lang: str):
    """(simge, ipucu metni, css sinifi) ya da dogrulama yoksa None.

    Kaynak: extra.checksum / extra.merkle (ok | warn | mismatch). En agir
    durum simgeyi ve rengi belirler; ipucu tum dogrulama notlarini tasir.
    """
    parts: list[str] = []
    worst = 0
    chk = str(job.get("checksum") or "")
    if chk == "mismatch":
        parts.append(t("err_checksum_mismatch", lang))
        worst = 3
    elif chk == "warn":
        parts.append(t("chk_warn", lang))
        worst = max(worst, 2)
    elif chk:
        algos = ", ".join(job.get("verified") or []) or "sha256"
        parts.append(t("chk_ok", lang, a=algos))
        worst = max(worst, 1)
    merkle = str(job.get("merkle") or "")
    if merkle == "mismatch":
        parts.append(t("err_merkle_mismatch", lang))
        worst = 3
    elif merkle == "warn":
        parts.append(t("chk_merkle_warn", lang))
        worst = max(worst, 2)
    elif merkle:              # ok / repaired / repairing
        parts.append(t("merkle_ok", lang))
        worst = max(worst, 1)
    warn = str(job.get("warning") or "")
    if warn:                  # or. soft_404: dosya yerinde, yalnizca uyari
        parts.append(t("warn_" + warn, lang))
        worst = max(worst, 2)
    if not parts:
        return None
    symbol, css = _BADGE[worst]
    return symbol, " · ".join(parts), css


def history_when(finished_at) -> str:
    """Gecmis satiri icin yerel zaman damgasi (bos ise bos)."""
    try:
        ts = float(finished_at or 0)
    except (TypeError, ValueError):
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def history_meta(rec: dict, lang: str) -> str:
    state = str(rec.get("state") or "")
    return t("hist_meta", lang,
             state=t("st_" + state, lang) if state in HIST_STATES else state,
             size=util.human_size(rec.get("bytes") or 0),
             date=history_when(rec.get("finished_at")),
             host=rec.get("host") or "—")


def history_links(rec: dict, lang: str) -> str:
    return t("hist_links", lang,
             avg=f"{float(rec.get('avg_links') or 0):.1f}",
             peak=int(rec.get("peak_links") or 0))


def history_stats_line(stats: dict, lang: str) -> str:
    """Ozet + sunucu basina ilk uc satir (web/CLI ile ayni bicim)."""
    out = [t("hist_stats", lang, n=stats.get("count", 0),
             bytes=util.human_size(stats.get("bytes", 0)))]
    for h in (stats.get("hosts") or [])[:3]:
        out.append(t("hist_host_line", lang, host=h.get("host", ""),
                     n=h.get("count", 0),
                     links=f"{float(h.get('avg_links') or 0):.1f}",
                     speed=util.human_size(h.get("avg_speed") or 0)
                     + t("per_sec", lang)))
    return "  ·  ".join(out)


def host_line(h: dict, lang: str) -> str:
    band = t("rtt_" + h["rtt_band"], lang) if h.get("rtt_band") else "?"
    seen = time.strftime("%Y-%m-%d",
                         time.localtime(h.get("last_seen") or 0))
    return t("hosts_row", lang, host=h.get("host", ""),
             links=h.get("links", 0), rtt=band,
             speed=util.human_size(h.get("per_link_bps") or 0),
             samples=h.get("samples", 0), seen=seen)
