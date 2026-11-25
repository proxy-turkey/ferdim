"""`python3 -m benchmarks`: performans kapisi (ROADMAP 1.13).

Kullanim:
    python3 -m benchmarks                     # tam kosu + baseline karsilastirma
    python3 -m benchmarks --quick             # kucuk isler (karsilastirma yok)
    python3 -m benchmarks --only queue        # tek paket (tekrarlanabilir)
    python3 -m benchmarks --json bench.json   # makine okunur ciktisi
    python3 -m benchmarks --update-baseline   # baseline.json'u yeniden yaz

Cikis kodu 1: baseline'a gore %20'den fazla gerileme (quick/update haric).
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

from . import compare, suites

BASELINE_PATH = Path(__file__).with_name("baseline.json")

SUITES = {
    "engine": suites.bench_engine,
    "chunker": suites.bench_chunker,
    "queue": suites.bench_queue,
    "idle": suites.bench_idle,
}


def _minor(version) -> str:
    """'3.14.10' -> '3.14'; mutlak olcumler ayni minor surumde karsilastirilir."""
    parts = str(version or "").split(".")
    return ".".join(parts[:2])


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_baseline(path: Path, record: dict) -> None:
    data = {
        "version": compare.BASELINE_VERSION,
        "generated": record["generated"],
        "python": record["python"],
        "platform": record["platform"],
        "calibration": record["calibration"],
        "metrics": record["metrics"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", "utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m benchmarks",
        description="FerDim performans olcumleri ve baseline kapisi.")
    ap.add_argument("--quick", action="store_true",
                    help="kucuk is boylari; karsilastirma atlanir")
    ap.add_argument("--update-baseline", action="store_true",
                    help="baseline.json'u bu kosunun olcumleriyle yenile")
    ap.add_argument("--json", metavar="PATH",
                    help="sonuclari + kararlari JSON olarak yaz")
    ap.add_argument("--only", action="append", choices=sorted(SUITES),
                    metavar="NAME",
                    help="yalniz bu paketi calistir (%s)" % ", ".join(SUITES))
    ap.add_argument("--baseline", metavar="PATH", default=str(BASELINE_PATH),
                    help="karsilastirilacak baseline dosyasi")
    args = ap.parse_args(argv)
    if args.update_baseline and args.quick:
        ap.error("--update-baseline tam kosu ister (--quick ile kullanilmaz)")
    if args.update_baseline and args.only:
        ap.error("--update-baseline tum paketleri ister (--only kullanilmaz)")

    names = [n for n in SUITES if not args.only or n in args.only]
    print("FerDim benchmarks  python %s  %s"
          % (platform.python_version(), platform.system().lower()))
    t_start = time.perf_counter()
    calib = suites.calibration()
    print("calibration: %.1f units/s" % calib)
    metrics = {}
    for name in names:
        t0 = time.perf_counter()
        print("[%s] ..." % name, end="", flush=True)
        try:
            metrics.update(SUITES[name](args.quick))
        except Exception as exc:
            print("\n[%s] HATA: %s" % (name, exc))
            return 1
        print(" %.1f sn" % (time.perf_counter() - t0), flush=True)
    elapsed = time.perf_counter() - t_start

    record = {
        "generated": _now(),
        "python": platform.python_version(),
        "platform": platform.system().lower(),
        "calibration": calib,
        "metrics": metrics,
    }
    verdicts: list = []
    failed = False
    if args.update_baseline:
        _write_baseline(Path(args.baseline), record)
        print("\nbaseline yazildi: %s" % args.baseline)
    elif args.quick:
        print("\nquick kosu: baseline karsilastirmasi atlandi")
    else:
        path = Path(args.baseline)
        try:
            baseline = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            print("\nbaseline okunamadi: %s (%s)" % (path, exc))
            return 1
        if int(baseline.get("version") or 0) != compare.BASELINE_VERSION:
            print("\nbaseline surumu %s, beklenen %s: --update-baseline ile "
                  "yenile" % (baseline.get("version"),
                              compare.BASELINE_VERSION))
            return 1
        bcalib = float(baseline.get("calibration") or 0.0)
        factor = (calib / bcalib) if bcalib > 0 else 1.0
        absolute_ok = (str(baseline.get("platform")) == record["platform"]
                       and _minor(baseline.get("python"))
                       == _minor(record["python"]))
        verdicts = compare.compare(baseline.get("metrics") or {}, metrics,
                                   factor, absolute_ok)
        print()
        print(compare.format_table(verdicts))
        if not absolute_ok:
            print("\nnot: RSS/bosta CPU farkli ortamda karsilastirilmaz "
                  "(ayni platform + Python minor surumu gerekir)")
        failed = compare.failed(verdicts)

    if args.json:
        out = dict(record, elapsed_s=round(elapsed, 1), verdicts=verdicts,
                   failed=failed)
        Path(args.json).write_text(json.dumps(out, indent=2) + "\n", "utf-8")
        print("\njson: %s" % args.json)

    print("\ntoplam: %.1f sn" % elapsed)
    if args.update_baseline or args.quick:
        return 0
    bad = [v for v in verdicts if v["status"] == "fail"]
    if failed:
        print("GERILEME: %s" % ", ".join(v["label"] for v in bad))
        return 1
    print("tamam: metrikler baseline sinirlari icinde")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
