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


class StatsWindow:
    """Kuyruk karelerini pencereli gorunume indirger (Gtk'dan bagimsiz).

    Sozlesme (`qm.stats`): tam kare `full=True` (jobs = ilk `limit` is),
    delta `full=False` (jobs = yalniz degisenler, `removed` = o revizyondan
    sonra silinen kimlikler). 10 000 islik kuyrukta her saniye tam kare
    cekmek ~6-7 MB demek: burada delta birlestirilir, pencere `limit` isi
    asmaz, tasarsa `full_pending` isareti verilir (cagiran tam kare ister).

    `params` dogrudan `stats` komutunun parametreleridir; `apply` yalniz
    DEGISEN satirlari (`upsert`) ve dusen kimlikleri (`removed`) dondurur:
    cagiran widget'lari kimlige gore yerinde gunceller (odak korunur)."""

    def __init__(self, limit: int = 500):
        self.limit = max(1, int(limit))
        self.jobs: dict[str, dict] = {}     # kimlik -> is
        self.rev = 0
        self.full_pending = True            # sonraki kare tam istensin
        self.total = 0                  # kuyrugun tamami (pencere dahil)

    @property
    def params(self) -> dict:
        """`stats` komut parametreleri: ilk kare tam, sonrasi delta."""
        out: dict = {"limit": self.limit}
        if self.rev and not self.full_pending:
            out["since"] = self.rev
        return out

    def apply(self, st: dict) -> dict:
        """Kareyi uygular; `upsert`/`removed`/`total` dondurur."""
        incoming = [j for j in (st.get("jobs") or ())
                    if isinstance(j, dict) and j.get("id")]
        rev = st.get("rev")
        full = bool(st.get("full")) or not isinstance(rev, int)
        if full:
            # tam kare: pencere sunucunun sayfasi olur, fazlasi duser
            keep = {j["id"] for j in incoming}
            removed = [jid for jid in self.jobs if jid not in keep]
            upsert = incoming
        else:
            gone = set(st.get("removed") or ())
            removed = [jid for jid in self.jobs if jid in gone]
            upsert = []
        # once dusenler (yer acilir), sonra gelenler
        for jid in removed:
            del self.jobs[jid]
        if not full:
            upsert = [j for j in incoming if self._fits(j["id"])]
        for job in upsert:
            self.jobs[job["id"]] = job
        self.rev = rev if isinstance(rev, int) else 0
        self.full_pending = False
        total = st.get("total_jobs")
        self.total = total if isinstance(total, int) else len(self.jobs)
        if len(self.jobs) > self.limit or self.total < len(self.jobs):
            # pencere tasti ya da sayim tutmuyor (kacan kare): kendini duzelt
            self.rev = 0
            self.full_pending = True
        elif st.get("removed") and self.total > len(self.jobs):
            # pencerede yer bosaldi: siradaki is ancak TAM karede gelir
            # (delta yalniz degisenleri tasir), bir sonraki kare tam olsun
            self.rev = 0
            self.full_pending = True
        return {"upsert": upsert, "removed": removed, "total": self.total}

    def _fits(self, jid: str) -> bool:
        """Deltada pencere disi is: yalniz bilinen ya da yer varsa."""
        return jid in self.jobs or len(self.jobs) < self.limit
