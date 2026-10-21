#!/bin/bash
# FerDim kurulumu (kullanici seviyesi, Fedora)
# Kullanim: bash scripts/install.sh [--uninstall] [--purge]
set -euo pipefail

SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
LIB="$HOME/.local/lib/ferdim"
BIN="$HOME/.local/bin"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
APPS="$HOME/.local/share/applications"
EXT_ID="ferdim@ferdim.app"

msg() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }

uninstall() {
  msg "FerDim kaldiriliyor..."
  rm -rf "$LIB"
  rm -f "$BIN/ferdim" "$BIN/ferdim-nm" "$BIN/ferdim-gui"
  rm -f "$ICONS/ferdim.svg" "$APPS/ferdim.desktop"
  rm -f "$HOME/.mozilla/native-messaging-hosts/ferdim.json"
  rm -f "$HOME/.var/app/org.mozilla.firefox/.mozilla/native-messaging-hosts/ferdim.json"
  rm -f "$HOME/.librewolf/native-messaging-hosts/ferdim.json"
  if [ "${1:-}" = "--purge" ]; then
    msg "Kullanici verileri de siliniyor (kuyruk, ayarlar)"
    rm -rf "$HOME/.local/state/ferdim" "$HOME/.config/ferdim"
  else
    msg "Kullanici verileri korundu (~/.local/state/ferdim)"
  fi
  msg "Kaldirildi. Firefox'i yeniden baslatmayi unutmayin."
  exit 0
}

[ "${1:-}" = "--uninstall" ] && uninstall "${2:-}"

command -v python3 >/dev/null || { echo "python3 bulunamadi"; exit 1; }

msg "FerDim dosyalari kopyalaniyor -> $LIB"
mkdir -p "$LIB" "$BIN" "$ICONS" "$APPS"
rm -rf "$LIB.bak"; [ -d "$LIB" ] && mv "$LIB" "$LIB.bak"
mkdir -p "$LIB"
cp -r "$SRC/ferdim" "$LIB/ferdim"
cp -r "$SRC/web" "$LIB/web"
rm -rf "$LIB.bak"

msg "Calistirilabilirler -> $BIN"
install -m 755 "$SRC/bin/ferdim" "$BIN/ferdim"
install -m 755 "$SRC/bin/ferdim-nm" "$BIN/ferdim-nm"
install -m 755 "$SRC/bin/ferdim-gui" "$BIN/ferdim-gui"

msg "Masaustu entegrasyonu"
install -m 644 "$SRC/scripts/icons/ferdim.svg" "$ICONS/ferdim.svg"
sed "s|@BIN@|$BIN/ferdim-gui|g" "$SRC/scripts/ferdim.desktop" > "$APPS/ferdim.desktop"
chmod 644 "$APPS/ferdim.desktop"

# Firefox native-messaging-host manifesti (RPM + Flatpak + LibreWolf kokleri)
NM_JSON='"name": "ferdim",
  "description": "FerDim Download Manager entegrasyonu",
  "path": "'"$BIN"'/ferdim-nm",
  "type": "stdio",
  "allowed_extensions": ["'"$EXT_ID"'"]'
for root in \
  "$HOME/.mozilla" \
  "$HOME/.var/app/org.mozilla.firefox/.mozilla" \
  "$HOME/.librewolf" \
  "$HOME/.var/app/io.gitlab.librewolfCommunity/.librewolf"; do
  if [ -d "$(dirname "$root")" ]; then
    mkdir -p "$root/native-messaging-hosts"
    printf '{\n%s\n}\n' "$NM_JSON" > "$root/native-messaging-hosts/ferdim.json"
    msg "Native host kaydi: $root/native-messaging-hosts/ferdim.json"
  fi
done

# Eklenti paketi (about:debugging / web-ext sign icin)
mkdir -p "$HOME/.local/share/ferdim"
if command -v zip >/dev/null; then
  (cd "$SRC/extension" && zip -qr "$HOME/.local/share/ferdim/ferdim-extension.zip" .)
  msg "Eklenti paketi: ~/.local/share/ferdim/ferdim-extension.zip"
fi

# systemd kullanici servisi (opsiyonel; otomatik baslatma)
mkdir -p "$HOME/.config/systemd/user"
sed "s|@BIN@|$BIN/ferdim|g" "$SRC/scripts/systemd/ferdim.service" \
  > "$HOME/.config/systemd/user/ferdim.service"

echo
msg "Kurulum tamamlandi!"
echo "  Komut satiri : $BIN/ferdim add <URL>"
echo "  Servis       : systemctl --user enable --now ferdim"
echo "                 (veya elle: ferdim daemon)"
echo "  Web arayuzu  : ferdim web      (http://127.0.0.1:8378)"
echo "  GTK arayuzu  : $BIN/ferdim-gui  (python3-gobject gerekir)"
echo
echo "Firefox eklentisi:"
echo "  1) Firefox'ta about:debugging#/runtime/this-firefox acin"
echo "  2) 'Gecici Eklenti Yukle' -> ~/.local/share/ferdim/ferdim-extension.zip"
echo "     (kalici kurulum icin README'deki web-ext sign bolumune bakin)"
echo
echo "PATH'de ~/.local/bin yoksa 'export PATH=\$HOME/.local/bin:\$PATH' ekleyin."
