# FerDim GNOME Shell extension

Panel indicator and menu for the FerDim daemon: aggregate speed, active
job count, per-job Pause / Resume / Cancel and an "Open web UI" item.
The extension targets GNOME Shell 47–49 and has no preferences.

The extension talks to the `org.ferdim.Daemon1` service owned by
`ferdim-dbus`. Install the bridge and the D-Bus activation file first:
`scripts/install.sh` does this for user-level installs, and the Fedora
RPM / AUR packages ship `/usr/share/dbus-1/services/org.ferdim.Daemon.service`.

The extension is MIT licensed, like the rest of FerDim; the packaging
script copies the repository `LICENSE` into the bundle, so the
submitted archive carries the license text.

## Installation

From extensions.gnome.org, use the install button on the listing page
(the site builds the bundle itself) and confirm the shell dialog.

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
`/usr/share` path. A prebuilt bundle installs with
`gnome-extensions install --force <uuid>.shell-extension.zip`.

On Wayland, log out and back in (or restart GNOME Shell) before enabling.
The indicator appears in the top bar; if the daemon is not running the
bridge is started on demand through D-Bus activation.

## Packaging

```bash
bash scripts/pack_gnome_extension.sh            # -> build/
bash scripts/pack_gnome_extension.sh dist       # custom output directory
```

The script needs `gnome-extensions` (from the `gnome-shell` package),
runs `gnome-extensions pack`, and refuses to finish unless the bundle
contains exactly `metadata.json`, `extension.js` and `LICENSE` — the
review guidelines reject archives with build scripts, locales or unused
media. It also aborts when `metadata.json`'s `version-name` differs from
`ferdim.__version__`, so a tag can never ship a stale version.

## Publishing a release

1. Bump `version-name` in
   `ferdim@proxy-turkey.github.io/metadata.json` together with the
   other version spots. `extensions.gnome.org` sets the integer
   `version` field itself, which is why `metadata.json` does not contain
   one; `version-name` is the human-readable release string.
2. `python3 -m unittest tests.test_gnome_extension -v` and
   `bash scripts/pack_gnome_extension.sh` must pass.
3. Push the tag. `.github/workflows/gnome-ext.yml` packs the extension
   and attaches `ferdim@proxy-turkey.github.io.shell-extension.zip` to
   the GitHub release as a downloadable artifact.
4. Upload the same zip by hand at <https://extensions.gnome.org/upload/>
   while logged in, then submit it for review. Uploading to
   extensions.gnome.org requires an account session and has no supported
   credential API, so this step stays manual — the workflow deliberately
   stops after the release asset.

## Review compliance

The extension follows the extensions.gnome.org review guidelines:

- Nothing is created at module import or in the `Extension` constructor;
  `enable()` builds the indicator, connects signals and starts the 2 s
  refresh timer.
- Every signal connection and main-loop source is registered in one
  `Tracker`; `disable()` (via `destroy()`) disconnects and removes them
  all and drops the D-Bus proxy reference.
- No deprecated modules (`Lang`, `Mainloop`, `ByteArray`), no Gtk/Gdk/Adw
  imports, no GSettings schema, no preferences window.
- `metadata.json` carries only `uuid`, `name`, `description`,
  `shell-version`, `url` and `version-name`.
- `tests/test_gnome_extension.py` enforces the static rules and the
  bundle contents, and parses `extension.js` with `node --check` and
  `gjs -m`.

## Smoke test in a real shell (optional)

The unit tests are static; a headless GNOME Shell can load, disable and
re-enable the extension for real. Run it from an isolated prefix so the
session and the user's settings are untouched:

```bash
dir=$(mktemp -d)
export XDG_DATA_HOME=$dir/data XDG_CONFIG_HOME=$dir/config \
       XDG_CACHE_HOME=$dir/cache XDG_STATE_HOME=$dir/state \
       XDG_RUNTIME_DIR=$dir/run GSETTINGS_BACKEND=keyfile
mkdir -p "$XDG_DATA_HOME/gnome-shell/extensions" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
cp -r gnome-shell-extension/ferdim@proxy-turkey.github.io \
  "$XDG_DATA_HOME/gnome-shell/extensions/"
gsettings set org.gnome.shell enabled-extensions \
  "['ferdim@proxy-turkey.github.io']"
dbus-run-session -- gnome-shell --headless --no-x11 &
sleep 6
gnome-extensions info ferdim@proxy-turkey.github.io   # Enabled: Yes, State: ACTIVE
gnome-extensions disable ferdim@proxy-turkey.github.io
gnome-extensions enable ferdim@proxy-turkey.github.io
```

`State: ERROR` means `enable()` threw — the shell log (stderr) carries
the JS stack. Reversing the order (`disable()` then `enable()`) must
stay clean: that is the review requirement the static test cannot cover.
