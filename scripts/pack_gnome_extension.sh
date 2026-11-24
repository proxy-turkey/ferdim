#!/usr/bin/env bash
# FerDim GNOME Shell eklentisini extensions.gnome.org'a yuklenebilir tek
# zip olarak paketler (GNOME inceleme kurallari: yalnizca gerekli dosyalar).
#
# Kullanim: bash scripts/pack_gnome_extension.sh [CIKTI_DIZINI]
# Cikti   : build/ferdim@proxy-turkey.github.io.shell-extension.zip
#
# gnome-extensions (gnome-shell paketi) gerekir. Zip'in icerigi burada
# dogrulanir: metadata.json + extension.js + LICENSE disinda dosya yoksa
# ve version-name ferdim.__version__ ile ayniysa betik basarili olur.
set -euo pipefail

UUID="ferdim@proxy-turkey.github.io"
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
EXT_DIR="$SRC/gnome-shell-extension/$UUID"
OUT_DIR="${1:-$SRC/build}"
ZIP="$OUT_DIR/$UUID.shell-extension.zip"

command -v gnome-extensions >/dev/null 2>&1 || {
  echo "gnome-extensions bulunamadi (gnome-shell paketi gerekli)" >&2
  exit 1
}
for f in "$EXT_DIR/extension.js" "$EXT_DIR/metadata.json" "$SRC/LICENSE"; do
  [ -f "$f" ] || { echo "eksik dosya: $f" >&2; exit 1; }
done

# metadata.json version-name surumle ayni olmali; surum bump'inda guncellenir.
PYTHONPATH="$SRC${PYTHONPATH:+:$PYTHONPATH}" python3 - "$EXT_DIR/metadata.json" <<'PY'
import json
import sys

import ferdim

with open(sys.argv[1], encoding="utf-8") as fh:
    meta = json.load(fh)
name = meta.get("version-name")
if name != ferdim.__version__:
    sys.exit(f"metadata.json version-name {name!r} != "
             f"ferdim.__version__ {ferdim.__version__!r}")
print(f"version-name: {name}")
PY

mkdir -p "$OUT_DIR"
rm -f "$ZIP"
# LICENSE depoda tek yerde durur; zip'e kopyalanir (EGO lisans dosyasi ister).
gnome-extensions pack -f --extra-source="$SRC/LICENSE" \
  --out-dir="$OUT_DIR" "$EXT_DIR"

# Zip yalnizca beklenen dosyalari icermeli; fazlasi incelemede reddedilir.
python3 - "$ZIP" <<'PY'
import sys
import zipfile

expected = {"metadata.json", "extension.js", "LICENSE"}
with zipfile.ZipFile(sys.argv[1]) as zf:
    names = set(zf.namelist())
    for name in sorted(names):
        print(f"  {name}")
unexpected = sorted(names - expected)
missing = sorted(expected - names)
if unexpected or missing:
    sys.exit(f"beklenmeyen={unexpected} eksik={missing}")
print(f"paket hazir: {sys.argv[1]}")
PY
