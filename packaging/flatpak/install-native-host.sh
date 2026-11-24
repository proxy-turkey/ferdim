#!/bin/sh
# Install the host-side half of FerDim's native messaging bridge so a
# sandboxed (Flatpak) Firefox can reach the sandboxed FerDim host through
# xdg-native-messaging-proxy.
#
# Installs:
#   ~/.local/bin/ferdim-nm-flatpak
#   ~/.mozilla/native-messaging-hosts/ferdim.json      (host manifest)
#   ~/.librewolf/native-messaging-hosts/ferdim.json    (when LibreWolf exists)
#
# It deliberately does NOT write into ~/.var/app/org.mozilla.firefox/:
# a confined Firefox cannot execute a host path, and the proxy resolves
# manifests from the host browser directories. See docs/flatpak.md.
set -eu

APP_ID=io.github.proxy_turkey.FerDim
EXT_ID=ferdim@ferdim.app
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BIN_DIR=${BIN_DIR:-"$HOME/.local/bin"}

if ! flatpak info "$APP_ID" >/dev/null 2>&1; then
    echo "warning: $APP_ID is not installed yet; the wrapper will fail until it is" >&2
fi

install -d "$BIN_DIR"
install -m 755 "$HERE/ferdim-nm-flatpak" "$BIN_DIR/ferdim-nm-flatpak"

manifest() {
    root=$1
    install -d "$root"
    cat > "$root/ferdim.json" <<EOF
{
  "name": "ferdim",
  "description": "FerDim Download Manager integration",
  "path": "$BIN_DIR/ferdim-nm-flatpak",
  "type": "stdio",
  "allowed_extensions": ["$EXT_ID"]
}
EOF
    echo "native host manifest: $root/ferdim.json"
}

manifest "$HOME/.mozilla/native-messaging-hosts"
if [ -d "$HOME/.librewolf" ]; then
    manifest "$HOME/.librewolf/native-messaging-hosts"
fi

cat <<EOF

Installed. Next steps:
  1) Install the xdg-native-messaging-proxy service on the host
     (org.freedesktop.NativeMessagingProxy, Firefox 157+ knows the client).
  2) In the Flatpak Firefox set
       widget.use-xdg-desktop-portal.native-messaging-proxy = 1
     in about:config (or leave it at 2 for autodetection).
  3) Install the FerDim WebExtension and restart Firefox.
EOF
