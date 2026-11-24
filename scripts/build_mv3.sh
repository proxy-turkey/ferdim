#!/usr/bin/env bash
# FerDim eklentisinin MV3 derlemesi: kaynagi denetler ve paketler.
#
# 1.7 ile eklenti kaynagi artik dogrudan manifest_version 3'tur
# (docs/mv3-spike.md). Bu betik eskiden MV2 kaynagindan bir MV3 "varyanti"
# uretiyordu; simdi yayinlanan kaynagi denetleyip dagitilabilir zip uretir:
#   1. manifest_version 3 kontrolu (MV2'ye sessiz donus olmasin)
#   2. web-ext lint --warnings-as-errors (0 hata / 0 uyari zorunlu)
#   3. python3 stdlib zipfile ile dist/ferdim-<surum>.zip
#
# Kullanim:
#   bash scripts/build_mv3.sh               # denetle + paketle
#   bash scripts/build_mv3.sh --lint-only   # yalnizca denetle
#   bash scripts/build_mv3.sh /tmp/mv3.zip  # belirli cikti yolu
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
src="$repo/extension"
if [ ! -f "$src/manifest.json" ]; then
  echo "hata: $src/manifest.json yok" >&2
  exit 1
fi

python3 - "$src/manifest.json" <<'PY'
import json
import sys

m = json.load(open(sys.argv[1], encoding="utf-8"))
if m.get("manifest_version") != 3:
    sys.exit("hata: manifest_version 3 bekleniyordu "
             "(kaynak MV2'ye donduyse docs/mv3-spike.md)")
for key in ("host_permissions", "action", "background"):
    if key not in m:
        sys.exit(f"hata: manifest.json icinde {key} yok")
if "scripting" not in m.get("permissions", []):
    sys.exit("hata: 'scripting' izni yok (baglanti tarama icin gerekli)")
print("kaynak : manifest_version 3, MV3 anahtarlari tamam")
PY

web_ext="${WEB_EXT:-web-ext@10.8.0}"
echo "lint   : npx -y $web_ext lint --source-dir $src --warnings-as-errors"
npx -y "$web_ext" lint --source-dir "$src" --warnings-as-errors

if [ "${1:-}" = "--lint-only" ]; then
  echo "bitti   : yalnizca denetim istendi, paket uretilmedi"
  exit 0
fi

version=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' \
  "$src/manifest.json")
out="${1:-$repo/dist/ferdim-$version.zip}"

OUT="$out" SRC="$src" python3 - <<'PY'
import os
import pathlib
import zipfile

src = pathlib.Path(os.environ["SRC"])
out = pathlib.Path(os.environ["OUT"])
out.parent.mkdir(parents=True, exist_ok=True)
if out.exists():
    out.unlink()

# Manifest kokte olmali; yol ayirici her zaman "/" (zip standardi).
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for p in sorted(src.rglob("*")):
        if p.is_file():
            z.write(p, p.relative_to(src).as_posix())

print(f"paket  : {out} ({out.stat().st_size} bayt)")
PY
