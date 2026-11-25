"""`python3 -m benchmarks`: performans kapisi (ROADMAP 1.13).

Kullanim:
    python3 -m benchmarks                     # tam kosu + baseline karsilastirma
    python3 -m benchmarks --quick             # kucuk isler (karsilastirma yok)
    python3 -m benchmarks --only queue        # tek paket (tekrarlanabilir)
    python3 -m benchmarks --json bench.json   # makine okunur ciktisi
    python3 -m benchmarks --update-baseline   # baseline.json'u yeniden yaz
    python3 -m benchmarks --baseline benchmarks/baseline-ci.json
                                              # CI kosucusu baseline'i

Baseline dosyasi YOKSA drift atlanir, yalnizca mutlak urun tabanlari
(floor) uygulanir: CI'da runner baseline'i (benchmarks/baseline-ci.json)
commit edilene kadar kapi yine de anlamlidir. Ortam alanlari (platform,
Python minor, cpu_count) uyusmuyorsa cekirdek/zamanlayiciya duyarli
metriklerde drift 'skip' olur; floor'lar her zaman uygulanir.

Cikis kodu 1: baseline'a gore %20'den fazla gerileme ya da mutlak urun
tabaninin (floor) ihlali (quick/update haric).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

from . import compare, suites

BASELINE_PATH = Path(__file__).with_name("baseline.json")
CI_BASELINE_NAME = "baseline-ci.json"

SUITES = {
    "engine": suites.bench_engine,
    "chunker": suites.bench_chunker,
    "queue": suites.bench_queue,
    "idle": suites.bench_idle,
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _spread(samples: dict) -> dict:
    """Metrik basina sacilim ozeti: min / medyan / max / goreli genislik."""
    out = {}
    for name, values in samples.items():
        vals = [float(v) for v in values if v is not None]
        if not vals:
            continue
        med = statistics.median(vals)
        out[name] = {
            "min": min(vals),
            "median": med,
            "max": max(vals),
            "rel": ((max(vals) - min(vals)) / med) if med else None,
        }
    return out


def _write_baseline(path: Path, record: dict) -> None:
    data = {
        "version": compare.BASELINE_VERSION,
        "generated": record["generated"],
        "python": record["python"],
        "platform": record["platform"],
        "cpu_count": record["cpu_count"],
        "calibration": record["calibration"],
        "io_calibration": record["io_calibration"],
        "metrics": record["metrics"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", "utf-8")


def _load_baseline(path: Path) -> dict | None:
    """Baseline'i okur; dosya YOKSA None (yalniz-floor kipi).

    Bozuk bir dosya sessizce yok sayilmaz: yanlis bir baseline'i
    "yok" saymak kapuyi gizlice kapatirdi, bu yuzden ValueError yukari
    cikar ve cagiran hata koduyla cikar.
    """
    try:
        raw = path.read_text("utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("baseline okunamadi: %s" % exc) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError("baseline JSON bozuk: %s" % exc) from exc
    if not isinstance(data, dict):
        raise ValueError("baseline bir JSON nesnesi degil")
    return data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m benchmarks",
        description="FerDim performans olcumleri ve baseline kapisi.")
    ap.add_argument("--quick", action="store_true",
                    help="kucuk is boylari; karsilastirma atlanir")
    ap.add_argument("--update-baseline", action="store_true",
                    help="baseline dosyasini bu kosunun olcumleriyle yenile")
    ap.add_argument("--json", metavar="PATH",
                    help="sonuclari, sacilimi ve kararlari JSON olarak yaz")
    ap.add_argument("--only", action="append", choices=sorted(SUITES),
                    metavar="NAME",
                    help="yalniz bu paketi calistir (%s)" % ", ".join(SUITES))
    ap.add_argument("--baseline", metavar="PATH", default=str(BASELINE_PATH),
                    help="karsilastirilacak baseline dosyasi "
                         "(CI: benchmarks/%s)" % CI_BASELINE_NAME)
    args = ap.parse_args(argv)
    if args.update_baseline and args.quick:
        ap.error("--update-baseline tam kosu ister (--quick ile kullanilmaz)")
    if args.update_baseline and args.only:
        ap.error("--update-baseline tum paketleri ister (--only kullanilmaz)")

    names = [n for n in SUITES if not args.only or n in args.only]
    cpu_count = os.cpu_count() or 0
    print("FerDim benchmarks  python %s  %s  cpus=%d"
          % (platform.python_version(), platform.system().lower(), cpu_count))
    t_start = time.perf_counter()
    cpu_calib = suites.calibration()
    io_calib = suites.io_calibration()
    print("calibration: cpu %.1f units/s  io %.1f units/s  cpus=%d"
          % (cpu_calib, io_calib, cpu_count))
    metrics = {}
    samples = {}
    for name in names:
        t0 = time.perf_counter()
        print("[%s] ..." % name, end="", flush=True)
        try:
            m, s = SUITES[name](args.quick)
        except Exception as exc:
            print("\n[%s] HATA: %s" % (name, exc))
            return 1
        metrics.update(m)
        samples.update(s)
        print(" %.1f sn" % (time.perf_counter() - t0), flush=True)
    elapsed = time.perf_counter() - t_start

    record = {
        "generated": _now(),
        "python": platform.python_version(),
        "platform": platform.system().lower(),
        "cpu_count": cpu_count,
        "calibration": cpu_calib,
        "io_calibration": io_calib,
        "metrics": metrics,
    }
    verdicts: list = []
    failed = False
    floors_only = False
    env_matches = True
    if args.update_baseline:
        _write_baseline(Path(args.baseline), record)
        print("\nbaseline yazildi: %s" % args.baseline)
    elif args.quick:
        print("\nquick kosu: baseline karsilastirmasi atlandi")
    else:
        path = Path(args.baseline)
        try:
            baseline = _load_baseline(path)
        except ValueError as exc:
            print("\nbaseline okunamadi: %s (%s)" % (path, exc))
            return 1
        if baseline is None:
            # CI'da runner baseline'i henuz commit edilmemis olabilir:
            # drift karsilastirmasi yapilamaz, urun tabanlari yine gecerli.
            floors_only = True
            print("\nbaseline yok: %s" % path)
            print("drift atlandi; yalnizca mutlak urun tabanlari (floor) "
                  "uygulanir")
            if path.name == CI_BASELINE_NAME:
                print("CI runner baseline'i icin .github/workflows/"
                      "bench-baseline.yml artigini commit edin")
            verdicts = compare.compare({}, metrics)
        else:
            if int(baseline.get("version") or 0) != compare.BASELINE_VERSION:
                print("\nbaseline surumu %s, beklenen %s: --update-baseline "
                      "ile yenile" % (baseline.get("version"),
                                      compare.BASELINE_VERSION))
                return 1
            env_matches = compare.environments_match(baseline, record)
            b_cpu = float(baseline.get("calibration") or 0.0)
            b_io = float(baseline.get("io_calibration") or 0.0)
            cpu_factor = (cpu_calib / b_cpu) if b_cpu > 0 else 1.0
            io_factor = (io_calib / b_io) if b_io > 0 else 1.0
            if b_io <= 0:
                print("not: baseline'da I/O kalibrasyonu yok (io factor 1.0); "
                      "--update-baseline ile yenile")
            verdicts = compare.compare(baseline.get("metrics") or {}, metrics,
                                       cpu_factor, io_factor, env_matches)
        print()
        print(compare.format_table(verdicts))
        if baseline is not None:
            if not env_matches:
                print("\nnot: baseline ortami farkli (platform/Python minor/"
                      "cpu_count); cekirdege duyarli metriklerde (motor + "
                      "mutlak) drift atlandi, floor'lar uygulandi")
            new = [v for v in verdicts if v["status"] == "new"]
            if new:
                print("not: %d metrik baseline'da yok (floor'lar uygulandi); "
                      "--update-baseline" % len(new))
        failed = compare.failed(verdicts)

    if args.json:
        out = dict(record, elapsed_s=round(elapsed, 1),
                   baseline=str(args.baseline), floors_only=floors_only,
                   samples=samples, spread=_spread(samples),
                   verdicts=verdicts, failed=failed)
        Path(args.json).write_text(json.dumps(out, indent=2) + "\n", "utf-8")
        print("\njson: %s" % args.json)

    print("\ntoplam: %.1f sn" % elapsed)
    if args.update_baseline or args.quick:
        return 0
    bad = [v for v in verdicts if v["status"] == "fail"]
    if failed:
        print("GERILEME: %s" % ", ".join(
            "%s (%s)" % (v["label"], v["note"]) for v in bad))
        return 1
    if floors_only:
        print("tamam: metrikler urun tabanlari icinde (drift atlandi)")
    else:
        print("tamam: metrikler baseline ve urun tabanlari icinde")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
