"""Baseline karsilastirma mantigi: saf fonksiyonlar, ferdim importu YOK.

Metrik aileleri:

- rate (yuksek iyi): makine hizina gore olceklenir. `scale` alani hangi
  kalibrasyonun kullanilacagini soyler:
    cpu -> saf hesaplama isi (parcalayici, kuyruk yukleme, stats())
    io  -> cekirdek/depolama yolu (loopback aktarim, fsync'li kuyruk yazimi)
- latency (dusuk iyi): ms cinsinden; cpu kalibrasyonuyla olceklenir.
- absolute (dusuk iyi): RSS ve bosta CPU; kalibrasyonla olceklenmez,
  kendi gurultu tabani ve mutlak urun tabani (floor) vardir.

Iki esik birlikte uygulanir:
1. drift: baseline'a gore %20'den fazla gerileme (hizda %20 dusus,
   gecikmede %20 artis, mutlak metrikte %20 artis).
2. floor: mutlak urun tabani (or. engine >= 150 MiB/s, bosta CPU <= %5,
   RSS <= 300 MiB, stats karesi <= 150 ms). Floor'lar ortam farkliysa
   yalnizca gerekcesi olanlarda atlanir (bkz. `compare`).

Farkli platform / Python minor surumunde RSS/CPU karsilastirilmaz
('skip'): bellekte ve bosta gucde yorumlayici ve isletim sistemi
farklari basarisizlik degil, ortam farkidir.
"""
from __future__ import annotations

from dataclasses import dataclass

REGRESSION = 0.20          # %20'den fazla gerileme basarisiz
CPU_FLOOR_PCT = 2.0        # bosta CPU: gurultu tabani (tek cekirdek yuzdesi)
BASELINE_VERSION = 1


@dataclass(frozen=True)
class Metric:
    """Tek metrigin tanimi.

    `scale`: rate/latency icin "cpu" | "io" (kalibrasyon secimi).
    `floor`: mutlak urun tabani; rate icin alt sinir (>=), latency ve
    absolute icin ust sinir (<=). Latency floor'u cpu faktoruyle
    olceklenir (daha hizli makinede daha kucuk gecikme beklenir).
    `noise`: absolute-dusuk metriklerde drift sinirinin altinda kalmasin
    (bosta CPU'nun 0'a yakin tabaninda %20 kurali gurultuye takilmasin).
    """

    label: str
    unit: str
    family: str              # "rate" | "latency" | "absolute"
    scale: str = ""          # "cpu" | "io" (rate/latency)
    floor: float | None = None
    noise: float = 0.0


# ad -> Metric. Adlar --json ciktisinda ve baseline.json'da aynen gorunur.
METRICS = {
    # engine: loopback + nokta (checkpoint) yolu -> I/O kalibrasyonu
    "engine_mib_s": Metric("engine throughput", "MiB/s", "rate", "io",
                           floor=150.0),
    # parcalayici saf hash/CPU isi
    "cdc_mib_s": Metric("chunker throughput", "MiB/s", "rate", "cpu"),
    # toplu ice aktarma: is olusturma (CPU) + tek fsync'li yazim (I/O)
    "queue_bulk_add_jobs_s": Metric("queue bulk add", "jobs/s", "rate", "io"),
    # yukleme: JSON ayristirma + is nesneleri (CPU)
    "queue_load_jobs_s": Metric("queue load", "jobs/s", "rate", "cpu"),
    # stats() gecikmesi (CPU)
    "stats_calls_s": Metric("stats() latency", "calls/s", "rate", "cpu"),
    # tam stats karesi: qm.stats() + hub.sock_frame JSON kodlamasi (CPU)
    "stats_frame_ms": Metric("stats frame", "ms", "latency", "cpu",
                             floor=150.0),
    # bosta daemon (bos kuyruk)
    "idle_cpu_pct": Metric("idle CPU", "% of a core", "absolute",
                           noise=CPU_FLOOR_PCT),
    "idle_rss_mib": Metric("idle RSS", "MiB", "absolute"),
    # 10 000 duraklatilmis is (tick bos tarama yapar)
    "idle_loaded_cpu_pct": Metric("idle CPU (paused queue)", "% of a core",
                                  "absolute", floor=5.0,
                                  noise=CPU_FLOOR_PCT),
    "idle_loaded_rss_mib": Metric("idle RSS (paused queue)", "MiB",
                                  "absolute", floor=300.0),
    # 10 000 isin bir kismi KUYRUK'ta: mesgul tick + yoklayici yolu
    "idle_queued_cpu_pct": Metric("idle CPU (queued work)", "% of a core",
                                  "absolute", floor=5.0,
                                  noise=CPU_FLOOR_PCT),
    "idle_queued_rss_mib": Metric("idle RSS (queued work)", "MiB",
                                  "absolute", floor=300.0),
    # ayni kuyruk + tek SSE istemcisi (saniyede bir stats karesi)
    "idle_sse_cpu_pct": Metric("idle CPU (SSE client)", "% of a core",
                               "absolute", noise=CPU_FLOOR_PCT),
    "idle_sse_rss_mib": Metric("idle RSS (SSE client)", "MiB",
                               "absolute", floor=300.0),
}

FLOOR_METRICS = tuple(n for n, m in METRICS.items() if m.floor is not None)


def _rate_verdict(name, spec, base, actual, factor):
    target = base * factor
    ratio = (actual / target) if target > 0 else 0.0
    reasons = []
    if ratio < 1.0 - REGRESSION:
        reasons.append("drift")
    if spec.floor is not None and actual < spec.floor:
        reasons.append("floor")
    return {
        "metric": name, "label": spec.label, "unit": spec.unit,
        "family": spec.family, "scale": spec.scale,
        "baseline": base, "target": target,
        "limit": target * (1.0 - REGRESSION), "floor": spec.floor,
        "actual": actual, "ratio": ratio,
        "status": "fail" if reasons else "ok",
        "note": "+".join(reasons),
    }


def _latency_verdict(name, spec, base, actual, factor):
    # dusuk iyi: hizli makinede ayni kod daha kisa surer
    target = base / factor if factor > 0 else base
    ratio = (target / actual) if actual > 0 else None
    reasons = []
    if actual > target * (1.0 + REGRESSION):
        reasons.append("drift")
    floor = (spec.floor / factor) if spec.floor is not None else None
    if floor is not None and actual > floor:
        reasons.append("floor")
    return {
        "metric": name, "label": spec.label, "unit": spec.unit,
        "family": spec.family, "scale": spec.scale,
        "baseline": base, "target": target,
        "limit": target * (1.0 + REGRESSION), "floor": floor,
        "actual": actual, "ratio": ratio,
        "status": "fail" if reasons else "ok",
        "note": "+".join(reasons),
    }


def _absolute_verdict(name, spec, base, actual):
    limit = base * (1.0 + REGRESSION)
    if spec.noise:
        limit = max(limit, spec.noise)
    ratio = (actual / base) if base > 0 else None
    reasons = []
    if actual > limit:
        reasons.append("drift")
    if spec.floor is not None and actual > spec.floor:
        reasons.append("floor")
    return {
        "metric": name, "label": spec.label, "unit": spec.unit,
        "family": spec.family, "scale": "",
        "baseline": base, "target": base, "limit": limit,
        "floor": spec.floor, "actual": actual, "ratio": ratio,
        "status": "fail" if reasons else "ok",
        "note": "+".join(reasons),
    }


def compare(baseline_metrics: dict, actual_metrics: dict,
            cpu_factor: float = 1.0, io_factor: float = 1.0,
            absolute_ok: bool = True) -> list[dict]:
    """Baseline ile guncel olcumleri karsilastirir; karar listesi doner.

    `cpu_factor` / `io_factor`: guncel/baseline kalibrasyon oranlari.
    `absolute_ok` False ise RSS/CPU mutlak metrikleri (ve floor'lari)
    'skip' olur: farkli ortamda karsilastirilamayan bir sayi
    basarisizliga donusmemeli.

    Baseline'da olmayan bir metrik 'new' sayilir (gecis) ama yine de
    floor'a tabidir: urun tabanlari baseline yenilenmeden de gecerlidir.
    Drift yalnizca baseline'da kayitli metrikler icin uygulanir.
    """
    factors = {"cpu": float(cpu_factor), "io": float(io_factor), "": 1.0}
    out = []
    for name, actual in actual_metrics.items():
        spec = METRICS.get(name)
        if spec is None:            # bilinmeyen ad: sessizce atla
            continue
        base = baseline_metrics.get(name)
        if spec.family == "absolute":
            if not absolute_ok:
                out.append({
                    "metric": name, "label": spec.label, "unit": spec.unit,
                    "family": spec.family, "scale": "",
                    "baseline": base, "target": None, "limit": None,
                    "floor": spec.floor, "actual": actual, "ratio": None,
                    "status": "skip", "note": "different environment",
                })
                continue
            if base is None:
                out.append(_missing_verdict(name, spec, actual))
            else:
                out.append(_absolute_verdict(name, spec, float(base),
                                             float(actual)))
            continue
        factor = factors.get(spec.scale, 1.0)
        if base is None:
            out.append(_missing_verdict(name, spec, actual, factor))
            continue
        if spec.family == "rate":
            out.append(_rate_verdict(name, spec, float(base), float(actual),
                                     factor))
        else:
            out.append(_latency_verdict(name, spec, float(base), float(actual),
                                        factor))
    return out


def _missing_verdict(name, spec, actual, factor: float = 1.0) -> dict:
    """Baseline'da yok: drift yok, floor varsa uygulanir."""
    reasons = []
    floor = spec.floor
    if spec.family == "latency" and floor is not None:
        floor = floor / factor if factor > 0 else floor
    if floor is not None:
        if spec.family == "rate" and actual < floor:
            reasons.append("floor")
        elif spec.family != "rate" and actual > floor:
            reasons.append("floor")
    return {
        "metric": name, "label": spec.label, "unit": spec.unit,
        "family": spec.family, "scale": spec.scale,
        "baseline": None, "target": None, "limit": None,
        "floor": floor, "actual": actual, "ratio": None,
        "status": "fail" if reasons else "new",
        "note": "+".join(reasons + ["not in baseline"]),
    }


def failed(verdicts) -> bool:
    """En az bir metrik esik veya taban ihlali mi?"""
    return any(v.get("status") == "fail" for v in verdicts)


def _fmt(value) -> str:
    if value is None:
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
    rows = [("metric", "unit", "baseline", "target", "limit", "floor",
             "current", "ratio", "status")]
    for v in verdicts:
        rows.append((
            v["label"], v["unit"], _fmt(v["baseline"]), _fmt(v["target"]),
            _fmt(v["limit"]), _fmt(v["floor"]), _fmt(v["actual"]),
            "-" if v["ratio"] is None else "%.2f" % v["ratio"],
            v["status"],
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for n, row in enumerate(rows):
        cells = []
        for i, cell in enumerate(row):
            # ilk kolon sola, gerisi saga yaslanir
            cells.append(cell.ljust(widths[i]) if i < 2
                         else cell.rjust(widths[i]))
        lines.append("  ".join(cells).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)
