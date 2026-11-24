# FerDim GNOME Shell extension

Panel indicator and menu for the FerDim daemon: aggregate speed, active
job count, per-job Pause / Resume / Cancel and an "Open web UI" item.
Version 1 targets GNOME Shell 47–49 and has no preferences.

The extension talks to the `org.ferdim.Daemon1` service owned by
`ferdim-dbus`. Install the bridge and the D-Bus activation file first:
`scripts/install.sh` does this for user-level installs, and the Fedora
RPM / AUR packages ship `/usr/share/dbus-1/services/org.ferdim.Daemon.service`.

## Installation

User-level installs (`bash scripts/install.sh`) copy the extension to
`~/.local/share/gnome-shell/extensions/ferdim@proxy-turkey.github.io/`
when `gnome-shell` is present. Enable it once:

```bash
gnome-extensions enable ferdim@proxy-turkey.github.io
```

RPM and AUR users install the package system-wide, so the extension is
not copied automatically (the package must stay desktop-agnostic). The
package ships the extension under `/usr/share/ferdim/gnome-shell-extension/`;
copy it into place and enable it:

```bash
mkdir -p ~/.local/share/gnome-shell/extensions
cp -r /usr/share/ferdim/gnome-shell-extension/ferdim@proxy-turkey.github.io \
  ~/.local/share/gnome-shell/extensions/
gnome-extensions enable ferdim@proxy-turkey.github.io
```

From a source checkout, use
`gnome-shell-extension/ferdim@proxy-turkey.github.io` instead of the
`/usr/share` path.

On Wayland, log out and back in (or restart GNOME Shell) before enabling.
The indicator appears in the top bar; if the daemon is not running the
bridge is started on demand through D-Bus activation.
