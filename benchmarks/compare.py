"""Baseline karsilastirma mantigi: saf fonksiyonlar, ferdim importu YOK.

Iki metrik ailesi vardir:

- hiz (rate): yuksek iyidir (MiB/s, is/sn, cagri/sn). Makineler arasi
  karsilastirma icin sabit kalibrasyon isine gore olceklenir:
  beklenen = baseline * (guncel_kalibrasyon / baseline_kalibrasyon).
  Boylece depolanan taban farkli CI kosucularinda da gecerlidir.
- mutlak (absolute): RSS ve bosta CPU yuzdesi. Kalibrasyonla olceklenmez
  (bellek ve boşta guc makine hizina bagli degildir); kendi esikleri
  vardir. Bosta CPU icin taban esik gurultuyu yutar: yalnizca
  max(taban * 1.2, CPU_FLOOR_PCT) asilirsa basarisiz olur. Farkli
  platformda / Python minor surumunde mutlak olcumler atlanir.

Esik: bir metrik baseline'a gore %20'den FAZLA gerilerse basarisiz.
Sinirda kalan deger (%20 tam) gecmistir.
"""
from __future__ import annotations

REGRESSION = 0.20          # %20'den fazla gerileme basarisiz
CPU_FLOOR_PCT = 2.0        # bosta CPU: gurultu tabani (tek cekirdek yuzdesi)
BASELINE_VERSION = 1

# ad -> (etiket, birim); hiz ailesi (yuksek iyi, kalibrasyonla olcekli)
RATE_METRICS = {
    "engine_mib_s": ("engine throughput", "MiB/s"),
    "cdc_mib_s": ("chunker throughput", "MiB/s"),
    "queue_save_jobs_s": ("queue save", "jobs/s"),
    "queue_load_jobs_s": ("queue load", "jobs/s"),
    "stats_calls_s": ("stats() latency", "calls/s"),
}

# ad -> (etiket, birim); mutlak aile (kucuk iyi, kendi esigi)
ABS_METRICS = {
    "idle_cpu_pct": ("idle CPU", "% of a core"),
    "idle_rss_mib": ("idle RSS", "MiB"),
    "idle_loaded_cpu_pct": ("idle CPU (loaded queue)", "% of a core"),
    "idle_loaded_rss_mib": ("idle RSS (loaded queue)", "MiB"),
}

# CPU metrikleri: taban esik uygulanir
CPU_METRICS = ("idle_cpu_pct", "idle_loaded_cpu_pct")


def spec_of(name: str):
    """Metrigin (aile, etiket, birim) bilgisi; bilinmeyen ad -> None."""
    if name in RATE_METRICS:
        label, unit = RATE_METRICS[name]
        return "rate", label, unit
    if name in ABS_METRICS:
        label, unit = ABS_METRICS[name]
        return "absolute", label, unit
    return None


def _rate_verdict(name, base, actual, factor):
    label, unit = RATE_METRICS[name]
    target = base * factor
    ratio = (actual / target) if target > 0 else 0.0
    return {
        "metric": name, "label": label, "unit": unit, "family": "rate",
        "baseline": base, "target": target,
        "limit": target * (1.0 - REGRESSION),
        "actual": actual, "ratio": ratio,
        "status": "ok" if ratio >= 1.0 - REGRESSION else "fail",
        "note": "",
    }


def _abs_verdict(name, base, actual):
    label, unit = ABS_METRICS[name]
    limit = base * (1.0 + REGRESSION)
    if name in CPU_METRICS:
        limit = max(limit, CPU_FLOOR_PCT)
    return {
        "metric": name, "label": label, "unit": unit, "family": "absolute",
        "baseline": base, "target": base, "limit": limit,
        "actual": actual, "ratio": (actual / base) if base > 0 else None,
        "status": "fail" if actual > limit else "ok",
        "note": "",
    }


def compare(baseline_metrics: dict, actual_metrics: dict,
            factor: float = 1.0, absolute_ok: bool = True) -> list[dict]:
    """Baseline ile guncel olcumleri karsilastirir; karar listesi doner.

    `factor`: guncel_kalibrasyon / baseline_kalibrasyon. `absolute_ok`
    False ise RSS/CPU mutlak metrikleri 'skip' olur (farkli ortam):
    karsilastirilamayan bir sayi basarisizliga donusmemeli.
    Sadece OLCULEN metrikler degerlendirilir; baseline'da olmayan yeni bir
    metrik 'new' (gecis) sayilir, boylece metrik eklemek kapuyi kirmaz.

    Her kararda `target` (normalize edilmis beklenen deger) ve `limit`
    (basarisizlik esigi) ayri tutulur; hiz metriklerinde limit hedefin
    %80'idir, mutlak metriklerde taban + CPU gurultu esigi.
    """
    out = []
    for name, actual in actual_metrics.items():
        spec = spec_of(name)
        if spec is None:            # bilinmeyen ad: sessizce atla
            continue
        family = spec[0]
        base = baseline_metrics.get(name)
        if base is None:
            out.append({
                "metric": name, "label": spec[1], "unit": spec[2],
                "family": family, "baseline": None, "target": None,
                "limit": None, "actual": actual, "ratio": None,
                "status": "new", "note": "not in baseline",
            })
            continue
        if family == "rate":
            out.append(_rate_verdict(name, float(base), float(actual),
                                     float(factor)))
            continue
        if not absolute_ok:
            out.append({
                "metric": name, "label": spec[1], "unit": spec[2],
                "family": family, "baseline": float(base),
                "target": float(base), "limit": None,
                "actual": float(actual), "ratio": None,
                "status": "skip", "note": "different environment",
            })
            continue
        out.append(_abs_verdict(name, float(base), float(actual)))
    return out


def failed(verdicts) -> bool:
    """En az bir metrik esigi asti mi?"""
    return any(v.get("status") == "fail" for v in verdicts)


def _fmt(value) -> str:
    if value is None:
        return "-"
    if value != value:              # NaN
        return "-"
    if abs(value) >= 1000:
        return "%.0f" % value
    if abs(value) >= 10:
        return "%.1f" % value
    if abs(value) >= 1:
        return "%.2f" % value
    return "%.3f" % value


def format_table(verdicts) -> str:
    """Okunabilir metin tablosu (CLI ciktisi)."""
    rows = [("metric", "unit", "baseline", "target", "limit", "current",
             "ratio", "status")]
    for v in verdicts:
        rows.append((
            v["label"], v["unit"], _fmt(v["baseline"]), _fmt(v["target"]),
            _fmt(v["limit"]), _fmt(v["actual"]),
            "-" if v["ratio"] is None else "%.2f" % v["ratio"],
            v["status"],
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for n, row in enumerate(rows):
        cells = []
        for i, cell in enumerate(row):
            # ilk kolon sola, sayilar saga yaslanir
            cells.append(cell.ljust(widths[i]) if i < 2
                         else cell.rjust(widths[i]))
        lines.append("  ".join(cells).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)
