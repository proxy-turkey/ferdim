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
   RSS <= 300 MiB, stats karesi <= 150 ms). Floor'lar ortam farkliysa da
   uygulanir: urun tabani kalibrasyona bagli degildir.

Ortam kapisi: `env_bound` metrikler (motor + tum mutlak metrikler)
cekirdek sayisina/zamanlayiciya duyarlidir; baseline'in ortami
(platform, Python minor surumu, cpu_count) guncel kosuyla uyusmuyorsa
DRIFT atlanir ('skip'), floor yine uygulanir. Kalibrasyonla olceklenen
CPU/I/O metrikleri ortamdan bagimsiz karsilastirilir. Boylece 2 vCPU'lu
bir CI kosucusu 12 cekirdekli gelistirme makinesinin motor hizina gore
yanlislikla kirmizi olmaz.
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
    `env_bound`: cekirdek sayisi/zamanlayiciya duyarli metrik; ortam
    uyusmuyorsa drift atlanir (floor yine uygulanir).
    """

    label: str
    unit: str
    family: str              # "rate" | "latency" | "absolute"
    scale: str = ""          # "cpu" | "io" (rate/latency)
    floor: float | None = None
    noise: float = 0.0
    env_bound: bool = False


# ad -> Metric. Adlar --json ciktisinda ve baseline.json'da aynen gorunur.
METRICS = {
    # engine: loopback + nokta (checkpoint) yolu; cekirdek sayisina ve
    # zamanlayiciya duyarli (2 vCPU kosucuda 12 cekirdek hedefi gecersiz)
    "engine_mib_s": Metric("engine throughput", "MiB/s", "rate", "io",
                           floor=150.0, env_bound=True),
    # parcalayici saf hash/CPU isi (kalibrasyonla olceklenir)
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
                           noise=CPU_FLOOR_PCT, env_bound=True),
    "idle_rss_mib": Metric("idle RSS", "MiB", "absolute", env_bound=True),
    # 10 000 duraklatilmis is (tick bos tarama yapar)
    "idle_loaded_cpu_pct": Metric("idle CPU (paused queue)", "% of a core",
                                  "absolute", floor=5.0,
                                  noise=CPU_FLOOR_PCT, env_bound=True),
    "idle_loaded_rss_mib": Metric("idle RSS (paused queue)", "MiB",
                                  "absolute", floor=300.0, env_bound=True),
    # 10 000 isin tamami KUYRUK'ta: mesgul tick yolu
    "idle_queued_cpu_pct": Metric("idle CPU (queued work)", "% of a core",
                                  "absolute", floor=5.0,
                                  noise=CPU_FLOOR_PCT, env_bound=True),
    "idle_queued_rss_mib": Metric("idle RSS (queued work)", "MiB",
                                  "absolute", floor=300.0, env_bound=True),
    # ayni kuyruk + tek SSE istemcisi (saniyede bir stats karesi)
    "idle_sse_cpu_pct": Metric("idle CPU (SSE client)", "% of a core",
                               "absolute", noise=CPU_FLOOR_PCT,
                               env_bound=True),
    "idle_sse_rss_mib": Metric("idle RSS (SSE client)", "MiB",
                               "absolute", floor=300.0, env_bound=True),
}

FLOOR_METRICS = tuple(n for n, m in METRICS.items() if m.floor is not None)
ENV_BOUND_METRICS = tuple(n for n, m in METRICS.items() if m.env_bound)


def minor_version(version) -> str:
    """'3.14.10' -> '3.14'; ortam karsilastirmasi minor surumde yapilir."""
    parts = str(version or "").split(".")
    return ".".join(parts[:2])


def _cpu_count(value):
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count > 0 else None


def environments_match(baseline: dict, current: dict) -> bool:
    """Baseline ortami guncel kosuyla ayni mi?

    platform + Python minor surumu + cpu_count karsilastirilir. Eski bir
    baseline'da cpu_count yoksa yalnizca bilinen alanlar karsilastirilir
    (bilinmeyen alan uyusmazlik sayilmaz).
    """
    if str(baseline.get("platform") or "") != str(current.get("platform") or ""):
        return False
    if minor_version(baseline.get("python")) != minor_version(current.get("python")):
        return False
    b_cpus = _cpu_count(baseline.get("cpu_count"))
    c_cpus = _cpu_count(current.get("cpu_count"))
    if b_cpus is not None and c_cpus is not None and b_cpus != c_cpus:
        return False
    return True


def _rate_verdict(name, spec, base, actual, factor, env_matches):
    target = base * factor
    ratio = (actual / target) if target > 0 else 0.0
    drift_ok = env_matches or not spec.env_bound
    reasons = []
    if drift_ok and ratio < 1.0 - REGRESSION:
        reasons.append("drift")
    if spec.floor is not None and actual < spec.floor:
        reasons.append("floor")
    if reasons:
        status = "fail"
    elif not drift_ok:
        status = "skip"
    else:
        status = "ok"
    note = list(reasons)
    if not drift_ok and not reasons:
        note.append("different environment")
    return {
        "metric": name, "label": spec.label, "unit": spec.unit,
        "family": spec.family, "scale": spec.scale,
        "baseline": base, "target": target,
        "limit": target * (1.0 - REGRESSION) if drift_ok else None,
        "floor": spec.floor, "actual": actual, "ratio": ratio,
        "status": status, "note": "+".join(note),
    }


def _latency_verdict(name, spec, base, actual, factor, env_matches):
    # dusuk iyi: hizli makinede ayni kod daha kisa surer
    target = base / factor if factor > 0 else base
    ratio = (target / actual) if actual > 0 else None
    drift_ok = env_matches or not spec.env_bound
    reasons = []
    if drift_ok and actual > target * (1.0 + REGRESSION):
        reasons.append("drift")
    floor = (spec.floor / factor) if spec.floor is not None else None
    if floor is not None and actual > floor:
        reasons.append("floor")
    if reasons:
        status = "fail"
    elif not drift_ok:
        status = "skip"
    else:
        status = "ok"
    note = list(reasons)
    if not drift_ok and not reasons:
        note.append("different environment")
    return {
        "metric": name, "label": spec.label, "unit": spec.unit,
        "family": spec.family, "scale": spec.scale,
        "baseline": base, "target": target,
        "limit": target * (1.0 + REGRESSION) if drift_ok else None,
        "floor": floor, "actual": actual, "ratio": ratio,
        "status": status, "note": "+".join(note),
    }


def _absolute_verdict(name, spec, base, actual, env_matches):
    ratio = (actual / base) if base > 0 else None
    reasons = []
    limit = None
    if env_matches:                      # drift yalniz ayni ortamda
        limit = base * (1.0 + REGRESSION)
        if spec.noise:
            limit = max(limit, spec.noise)
        if actual > limit:
            reasons.append("drift")
    if spec.floor is not None and actual > spec.floor:
        reasons.append("floor")
    if reasons:
        status = "fail"
    elif not env_matches:
        status = "skip"
    else:
        status = "ok"
    note = list(reasons)
    if not env_matches and not reasons:
        note.append("different environment")
    return {
        "metric": name, "label": spec.label, "unit": spec.unit,
        "family": spec.family, "scale": "",
        "baseline": base, "target": base, "limit": limit,
        "floor": spec.floor, "actual": actual, "ratio": ratio,
        "status": status, "note": "+".join(note),
    }


def compare(baseline_metrics: dict, actual_metrics: dict,
            cpu_factor: float = 1.0, io_factor: float = 1.0,
            env_matches: bool = True) -> list[dict]:
    """Baseline ile guncel olcumleri karsilastirir; karar listesi doner.

    `cpu_factor` / `io_factor`: guncel/baseline kalibrasyon oranlari.
    `env_matches` False ise `env_bound` metriklerde (motor + mutlak
    metrikler) drift 'skip' olur; floor'lar yine uygulanir. Kalibrasyonla
    olceklenen CPU/I/O metrikleri ortam farkindan etkilenmez.

    Baseline'da olmayan bir metrik 'new' sayilir (gecis) ama yine de
    floor'a tabidir: urun tabanlari baseline yenilenmeden de gecerlidir.
    Drift yalnizca baseline'da kayitli metrikler icin uygulanir. Bos
    baseline sozlugu "yalnizca floor" kipidir (CI'da runner baseline'i
    commit edilene kadar kullanilir).
    """
    factors = {"cpu": float(cpu_factor), "io": float(io_factor), "": 1.0}
    out = []
    for name, actual in actual_metrics.items():
        spec = METRICS.get(name)
        if spec is None:            # bilinmeyen ad: sessizce atla
            continue
        base = baseline_metrics.get(name)
        if base is None:
            factor = factors.get(spec.scale, 1.0)
            out.append(_missing_verdict(name, spec, actual, factor))
            continue
        if spec.family == "absolute":
            out.append(_absolute_verdict(name, spec, float(base),
                                         float(actual), env_matches))
            continue
        factor = factors.get(spec.scale, 1.0)
        if spec.family == "rate":
            out.append(_rate_verdict(name, spec, float(base), float(actual),
                                     factor, env_matches))
        else:
            out.append(_latency_verdict(name, spec, float(base), float(actual),
                                        factor, env_matches))
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
