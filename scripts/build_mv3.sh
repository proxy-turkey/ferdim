#!/usr/bin/env bash
# FerDim eklentisi icin MV3 "spike" derlemesi.
#
# extension/ dizinini gecici bir cikti dizinine kopyalar, manifest.json'u
# python3 stdlib ile MV3'e cevirir (kalici kaynak degismez) ve sonucu
# web-ext ile denetler. Amac onceden uyumlulugu olcmek; yayin MV2'den
# yapilmaya devam eder (docs/mv3-spike.md).
#
# Kullanim:
#   bash scripts/build_mv3.sh            # gecici dizin
#   bash scripts/build_mv3.sh /tmp/mv3   # belirli dizin (uzerine kopyalanir)
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
src="$repo/extension"
if [ ! -f "$src/manifest.json" ]; then
  echo "hata: $src/manifest.json yok" >&2
  exit 1
fi
out="${1:-$(mktemp -d "${TMPDIR:-/tmp}/ferdim-mv3-XXXXXX")}"

mkdir -p "$out"
cp -a "$src/." "$out/"

SRC="$src" OUT="$out" python3 - <<'PY'
import json
import os
import pathlib
import sys

src = pathlib.Path(os.environ["SRC"])
out = pathlib.Path(os.environ["OUT"])
orig = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
m = json.loads(json.dumps(orig))          # derin kopya

changes = []

m["manifest_version"] = 3
changes.append("manifest_version 2 -> 3")

perms = [p for p in m.get("permissions", []) if "<" not in p and "://" not in p]
hosts = list(m.get("host_permissions") or [])
for p in orig.get("permissions", []):
    if "<" in p or "://" in p:
        if p not in hosts:
            hosts.append(p)
m["permissions"] = perms
if hosts:
    m["host_permissions"] = hosts
    changes.append("host_permissions <- " + ", ".join(hosts))

action = m.pop("browser_action", None)
if action is not None:
    m["action"] = action
    changes.append("browser_action -> action")

# Firefox MV3 page_action anahtarini/API'sini korur (action'a birlestirmez),
# bu yuzden anahtar yerinde kalir; Chromium icin ayri bir donusum gerekir.
if "page_action" in m:
    changes.append("page_action korundu (Firefox MV3)")

bg = m.get("background")
if isinstance(bg, dict):
    if bg.pop("persistent", None) is not None:
        changes.append("background.persistent kaldirildi (MV3 hatasi olurdu)")
    else:
        # event page: Firefox MV3'te scripts destekli, service_worker yok
        changes.append("background.scripts korundu (event page)")
    m["background"] = bg

(out / "manifest.json").write_text(
    json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

print("MV3 varyanti :", out)
for line in changes:
    print("  -", line)
PY

web_ext="${WEB_EXT:-web-ext@10.8.0}"
# MV3 icin --warnings-as-errors kullanilmaz: bilinen ve belgelenmis
# uyari(lar) var (tabs.executeScript -> scripting API). Hata varsa komut
# yine sifirdan farkli kodla doner.
echo "lint         : npx -y $web_ext lint --source-dir $out"
npx -y "$web_ext" lint --source-dir "$out"
echo
echo "not: MV3'te 'tabs.executeScript' yok; grab ozelligi scripting API'sine"
echo "     tasinmali (docs/mv3-spike.md, 'Migration plan')."
