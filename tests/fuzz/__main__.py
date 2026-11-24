"""Fuzz kampanyasi CLI.

    python3 -m tests.fuzz --seconds 60 --seed 1
    python3 -m tests.fuzz --target dash --iterations 500 --seed 7
    python3 -m tests.fuzz --list

Cikis kodu: 0 -> yalniz bilinen siniflar, 1 -> yeni bulgu var.
Sure butcesi TUM hedefler arasinda paylasilir: 60 saniyelik CI isi her
hedefe dokunur.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .harness import KNOWN_PATH, known_suggestion, load_known, run_target
from .targets import TARGETS


def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python3 -m tests.fuzz",
        description="stdlib mutasyon fuzzer'i (deterministik)")
    ap.add_argument("--seed", type=int, default=1,
                    help="mutasyon tohumu (varsayilan 1)")
    ap.add_argument("--seconds", type=float, default=60.0,
                    help="TUM hedefler icin toplam sure butcesi")
    ap.add_argument("--iterations", type=int, default=None,
                    help="hedef basina girdi sayisi (sure yerine)")
    ap.add_argument("--target", action="append", choices=sorted(TARGETS),
                    default=None, help="yalniz bu hedef (tekrarlanabilir)")
    ap.add_argument("--timeout", type=float, default=2.0,
                    help="girdi basina sure siniri (saniye)")
    ap.add_argument("--mem-mb", type=float, default=0.0,
                    help="girdi basina bellek artisi siniri (MiB, 0=kapali)")
    ap.add_argument("--known", default=str(KNOWN_PATH),
                    help="bilinen bulgu sinifllari dosyasi")
    ap.add_argument("--no-known", action="store_true",
                    help="known.json'i yoksay (her sinif yeni sayilir)")
    ap.add_argument("--json", default=None,
                    help="bulgu raporunu bu dosyaya JSON olarak yaz")
    ap.add_argument("--list", action="store_true", help="hedefleri listele")
    ap.add_argument("--quiet", action="store_true",
                    help="yalniz ozet ve yeni bulgular")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    names = args.target or sorted(TARGETS)
    if args.list:
        for name in names:
            target = TARGETS[name]
            print(f"{name:9s} {len(target.seeds)} tohum  {target.note}")
        return 0
    known = {} if args.no_known else load_known(args.known)
    mem = int(args.mem_mb * 1024 * 1024) if args.mem_mb > 0 else None
    started = time.monotonic()
    deadline = started + args.seconds
    results = []
    for index, name in enumerate(names):
        remaining = max(0.0, deadline - time.monotonic())
        share = remaining / (len(names) - index)
        result = run_target(
            TARGETS[name], seed=args.seed,
            seconds=None if args.iterations else share,
            iterations=args.iterations, timeout=args.timeout,
            memory_limit=mem)
        results.append(result)
        if not args.quiet:
            print(f"[{name}] {result.inputs} girdi, {result.elapsed:.2f}s, "
                  f"{len(result.findings)} sinif")
    total_inputs = sum(r.inputs for r in results)
    new = [f for r in results for f in r.new(known)]
    hits = [f for r in results for f in r.known_hits(known)]
    for finding in hits:
        print(f"bilinen {finding.key} x{finding.count}")
    for finding in new:
        print()
        print("YENI " + finding.report())
        print("  known.json kaydi: " + json.dumps(
            known_suggestion(finding), ensure_ascii=True))
    elapsed = time.monotonic() - started
    print(f"\nfuzz: {len(names)} hedef, {total_inputs} girdi, {elapsed:.1f}s, "
          f"yeni sinif {len(new)}, bilinen sinif {len(hits)}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"seed": args.seed, "seconds": args.seconds,
                       "iterations": args.iterations,
                       "targets": [r.target for r in results],
                       "new": [known_suggestion(f) for f in new],
                       "findings": [
                           {"id": f.key, "count": f.count,
                            "detail": f.detail, "frames": list(f.frames),
                            "first_iter": f.first_iter,
                            "sample_hex": f.hex_sample(),
                            "known": f.key in known}
                           for r in results for f in r.findings.values()]},
                      fh, ensure_ascii=True, indent=2)
            fh.write("\n")
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
