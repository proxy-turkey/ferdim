#!/bin/bash
# Arayuz duman testleri. Gercek kullanici durumuna dokunmaz: gecici XDG
# kokunde yaltilmis daemon + yerel dosya sunucusu ile kosar.
# Bagimliliklar (istege bagli): GTK -> python3-gobject, xvfb-run;
# web -> python3-playwright ve `python3 -m playwright install chromium`.
# Kullanim: bash tests/ui/run.sh [web|gtk|all]     (varsayilan: all)
# UI_SHOTS=DIZIN verilirse web ekran goruntuleri orada birakilir.
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
WHAT="${1:-all}"
T="$(mktemp -d /tmp/ferdim-ui-XXXXXX)"
SRV_PID=""

cleanup() {
  [ -f "$T/state/ferdim/ferdim.pid" ] && \
    kill "$(cat "$T/state/ferdim/ferdim.pid")" 2>/dev/null || true
  [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null || true
  [ -z "${UI_SHOTS:-}" ] && rm -rf "$T" || rm -rf "$T"
}
trap cleanup EXIT

mkdir -p "$T/state" "$T/config/ferdim" "$T/dl" "$T/www" "$T/home"
printf '{"download_dir": "%s/dl", "web_port": 8390, "language": "tr"}\n' "$T" \
  > "$T/config/ferdim/config.json"
python3 "$REPO/tests/ui/srv.py" "$T/www" 0 > "$T/srv.port" & SRV_PID=$!
for _ in $(seq 1 50); do [ -s "$T/srv.port" ] && break; sleep 0.1; done
SP="$(head -1 "$T/srv.port")"

# Ev dizini de gecici kokte: HTTP config-set yalniz ev dizininin altindaki
# yollari kabul eder (bkz ferdim/api.py _config_http_guard). Boylece web
# ayarlari testte gercek ~/.config'e dokunmaz.
( cd / && HOME="$T/home" XDG_STATE_HOME="$T/state" \
    XDG_CONFIG_HOME="$T/config" PYTHONPATH="$REPO" \
    exec python3 -m ferdim.daemon > "$T/daemon.out" 2>&1 ) &
# daemon 8390'dan baslayip bos port secer; gercek portu dosyaya yazar
for _ in $(seq 1 60); do [ -s "$T/state/ferdim/port" ] && break; sleep 0.2; done
if [ ! -s "$T/state/ferdim/port" ]; then
  echo "daemon baslamadi"; cat "$T/daemon.out"; exit 1
fi
DP="$(cat "$T/state/ferdim/port")"
export XDG_STATE_HOME="$T/state" XDG_CONFIG_HOME="$T/config" PYTHONPATH="$REPO"
RC=0

if [ "$WHAT" = web ] || [ "$WHAT" = all ]; then
  # HOME yalniz daemon'a verildi (yukarida): HTTP config-set ev dizininin
  # altindaki yollari kabul eder, testte bu gecici kok olur. Test sureci ve
  # Chromium gercek HOME'u kullanir (playwright ikilisi orada kurulu).
  ( cd "$REPO" && WATCH_BASE="$T/home" PORT="$DP" \
      SRV="http://127.0.0.1:$SP" \
      SLOW="http://127.0.0.1:$SP/slow" TOKEN_PATH="$T/state/ferdim/token" \
      SOCK="$T/state/ferdim/ferdim.sock" \
      timeout 240 python3 tests/ui/web_live.py ) || RC=1
fi
if [ "$WHAT" = gtk ] || [ "$WHAT" = all ]; then
  ( cd "$REPO" && timeout 90 xvfb-run -a python3 tests/ui/gtk_smoke.py ) || RC=1
fi
exit $RC
