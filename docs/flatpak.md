# Flatpak and Flathub

FerDim ships a Flatpak recipe for the GTK app, the CLI and the daemon it
spawns, plus the native-messaging bridge used by Firefox. The recipe is
built and linted in CI on every `v*` tag; uploads to Flathub are a human
step (see [Flathub submission](#flathub-submission)).

## Layout

| Path | Purpose |
|---|---|
| `packaging/flatpak/io.github.proxy_turkey.FerDim.yml` | Flathub manifest, `v1.6.0` tag archive + sha256 |
| `packaging/flatpak/dev/io.github.proxy_turkey.FerDim.yml` | same build from the working tree (`type: dir`) |
| `packaging/flatpak/io.github.proxy_turkey.FerDim.metainfo.xml` | AppStream metadata |
| `packaging/flatpak/io.github.proxy_turkey.FerDim.desktop` | desktop entry (English, app-id file name) |
| `packaging/flatpak/io.github.proxy_turkey.FerDim.svg` | app icon, copied from `scripts/icons/ferdim.svg` |
| `packaging/flatpak/ferdim-nm-flatpak` | host-side native-messaging wrapper |
| `packaging/flatpak/install-native-host.sh` | installs the wrapper + Firefox manifest |
| `.github/workflows/flatpak.yml` | build, lint, smoke test, bundle artifact |

## App id

```
io.github.proxy_turkey.FerDim
```

Flathub accepts a reverse-DNS id under a GitHub Pages domain
(`github.io`) when the repository is the upstream. D-Bus names cannot
contain a hyphen, so the GitHub user `proxy-turkey` is written
`proxy_turkey`. The id must match exactly in the manifest, the metainfo
(`<id>`), the desktop file name and the icon file name.

The capital letters in `FerDim` are legal but AppStream flags them as a
pedantic hint (`cid-contains-uppercase-letter`). Keeping the branded
spelling is deliberate.

## Runtime

`org.gnome.Platform//51` / `org.gnome.Sdk//51` (latest Flathub branch at
the time of writing; 49 and 50 are also published). The GNOME runtime
already contains Python 3, PyGObject 3.58 and GTK 4, so the app needs no
Python wheels and no pip at build time. `--runtime-version` appears in
both manifests and must be bumped together with the SDK install in CI.

## Sandbox permissions

```yaml
finish-args:
  - --share=network                 # engine, web UI, localhost API
  - --share=ipc                     # GTK under fallback X11
  - --socket=wayland
  - --socket=fallback-x11
  - --device=dri                    # GTK rendering
  - --filesystem=xdg-download       # only the downloads folder
  - --talk-name=org.freedesktop.Notifications
  - --own-name=org.ferdim.Daemon    # ferdim-dbus bridge
  - --own-name=app.ferdim.gui       # TEMPORARY, see code changes below
```

There is no home-directory permission: the queue, settings, token and the
Unix socket live under the sandbox's own XDG state directory, i.e.
`~/.var/app/io.github.proxy_turkey.FerDim/.local/state/ferdim/`
(`ferdim.sock`, `token`, `jobs/`, `history.db`). Downloads go to the
host's downloads folder through `xdg-download`. The daemon is started by
the GUI/CLI through the existing `ensure_daemon()` (`python -m
ferdim.daemon`); no systemd unit is used inside the sandbox.

The web UI binds `127.0.0.1:8378` and falls back to the next free port up
to `8397` if the host already runs a daemon, so the sandbox and a host
install can coexist. The token is readable from the sandbox state path
above.

`--filesystem=xdg-run/ferdim` is **not** needed: the socket is in the
private state directory, not in `$XDG_RUNTIME_DIR`. Sharing it would only
expose one instance's socket to the other installation, and their queue
state roots differ anyway.

## Local build

Install the runtime/SDK user-level and the Builder container (Builder is
only needed because `flatpak-builder` is not assumed on the host):

```bash
flatpak remote-add --user --if-not-exists flathub \
  https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user -y --noninteractive flathub \
  org.gnome.Platform//51 org.gnome.Sdk//51 org.flatpak.Builder
```

When the build is driven through `org.flatpak.Builder`, its
`flatpak-builder` looks at the **system** installation (the app's own
user installation is a private one). Install the SDK system-wide as well,
or run a host `flatpak-builder` instead:

```bash
flatpak install --system -y --noninteractive flathub org.gnome.Sdk//51
```

Build directory and state directory must be on the same filesystem. If
the build directory is outside `$HOME`, let Builder see it:

```bash
flatpak override --user --filesystem=/tmp/opencode org.flatpak.Builder
```

Then:

```bash
flatpak run --user --command=flatpak-builder org.flatpak.Builder \
  --state-dir=/tmp/opencode/flatpak-state \
  --force-clean --repo=/tmp/opencode/flatpak-repo \
  /tmp/opencode/flatpak-build \
  packaging/flatpak/dev/io.github.proxy_turkey.FerDim.yml

flatpak install --user -y --noninteractive \
  /tmp/opencode/flatpak-repo io.github.proxy_turkey.FerDim

flatpak run --user --command=ferdim io.github.proxy_turkey.FerDim --version
# FerDim 1.6.0

flatpak run --user io.github.proxy_turkey.FerDim   # GTK window (needs a display)
```

The Flathub (archive) manifest builds the same way; it downloads the tag
archive and verifies the sha256:

```bash
flatpak run --user --command=flatpak-builder org.flatpak.Builder \
  --state-dir=/tmp/opencode/flatpak-state \
  --force-clean --repo=/tmp/opencode/flatpak-repo \
  /tmp/opencode/flatpak-build \
  packaging/flatpak/io.github.proxy_turkey.FerDim.yml
```

## Checks

```bash
appstreamcli validate --no-net \
  packaging/flatpak/io.github.proxy_turkey.FerDim.metainfo.xml

desktop-file-validate \
  packaging/flatpak/io.github.proxy_turkey.FerDim.desktop

flatpak run --user --command=flatpak-builder-lint org.flatpak.Builder \
  manifest packaging/flatpak/io.github.proxy_turkey.FerDim.yml

flatpak run --user --command=flatpak-builder-lint org.flatpak.Builder \
  manifest packaging/flatpak/dev/io.github.proxy_turkey.FerDim.yml
```

The repository copy of the dev manifest lives in a `dev/` directory and
keeps the app-id file name, so the Flathub linter is happy with both.

## Flathub submission

Publishing is **not** automated. Flathub takes apps through a pull request
to the [`flathub/flathub`](https://github.com/flathub/flathub) repository:

1. Fork `flathub/flathub`, create a branch and a directory named exactly
   `io.github.proxy_turkey.FerDim/`.
2. Copy the manifest, metainfo, desktop file and icon into that directory
   (the `type: file` sources are resolved relative to the manifest).
   The manifest must stay named `io.github.proxy_turkey.FerDim.yml`.
3. Open a pull request from that branch. The Flathub reviewer checks the
   build, the linter and the app id ownership (the GitHub repository under
   `proxy-turkey/ferdim` is the proof for the `github.io` id). A maintainer
   must be available to answer the review.
4. After merge, Flathub builds and publishes the app; later releases are
   updates to the same directory (new tag URL + sha256 + metainfo release).

A tag push only produces the local `.flatpak` bundle artifact
(`.github/workflows/flatpak.yml`); it never pushes to Flathub.

## Native messaging for a sandboxed Firefox

A confined Firefox cannot see host `native-messaging-hosts` directories
and cannot execute host binaries, so it cannot start `/usr/bin/ferdim-nm`.
The current upstream mechanism is the separate D-Bus service
`org.freedesktop.NativeMessagingProxy`
([xdg-native-messaging-proxy](https://github.com/flatpak/xdg-native-messaging-proxy),
not part of xdg-desktop-portal); Firefox implements the client side and
selects it with:

```
widget.use-xdg-desktop-portal.native-messaging-proxy = 1   # 2 = autodetect
```

in `about:config` (Firefox 157+; the older WebExtensions portal was never
released upstream and only ever shipped as a distribution patch). The
proxy runs on the host, finds the manifest in the normal host browser
directories, and spawns the executable named there.

For FerDim the host executable is a small wrapper that enters the sandbox:

```sh
# packaging/flatpak/ferdim-nm-flatpak
exec flatpak run --command=ferdim-nm io.github.proxy_turkey.FerDim "$@"
```

`flatpak run` keeps stdin/stdout/stderr attached, so the 4-byte-LE length
+ JSON protocol passes through unchanged; the sandboxed `ferdim-nm`
connects to (or starts) the daemon in the same sandbox. `ferdim-nm-flatpak`
only writes to stderr on failure, never to stdout.

Install the wrapper and the manifest:

```bash
packaging/flatpak/install-native-host.sh
```

which writes `~/.local/bin/ferdim-nm-flatpak` and
`~/.mozilla/native-messaging-hosts/ferdim.json` (plus the LibreWolf
equivalent when that profile directory exists):

```json
{
  "name": "ferdim",
  "description": "FerDim Download Manager integration",
  "path": "/home/USER/.local/bin/ferdim-nm-flatpak",
  "type": "stdio",
  "allowed_extensions": ["ferdim@ferdim.app"]
}
```

The manifest name `ferdim` matches `NATIVE` in `extension/background.js`;
the allow-list carries the extension id from `extension/manifest.json`.

Notes:

- Do not put the manifest under
  `~/.var/app/org.mozilla.firefox/.mozilla/native-messaging-hosts/`: a
  binary inside the Firefox sandbox cannot launch a second Flatpak.
  That location only works for hosts that ship a binary inside the
  Firefox sandbox itself.
- The proxy trusts whatever manifest it finds; the upstream project calls
  this out as inherently unsafe for sandboxed callers. Enable it only with
  native hosts you trust.
- The extension still has to be installed in Firefox (signed build from
  AMO, or a temporary load from `about:debugging` for testing).

## Code changes needed (not in this packaging lane)

These are reported for `ferdim/` and were not edited here:

1. **GApplication id.** `ferdim/gui/app.py` uses
   `application_id="app.ferdim.gui"`. The Flatpak session bus only lets
   the app own its Flatpak id, so the GUI exits with
   `Failed to register: … ServiceUnknown` unless the manifest grants
   `--own-name=app.ferdim.gui` (the temporary workaround above). Align
   the id with `io.github.proxy_turkey.FerDim`, update
   `StartupWMClass` in the desktop file, and drop the extra
   `--own-name` from both manifests.
2. **D-Bus activation.** `data/org.ferdim.Daemon.service` points at an
   absolute `/app/bin/ferdim-dbus` only after install; the session bus on
   the host cannot activate a service file from inside the sandbox.
   Owning `org.ferdim.Daemon` works once the bridge is started (for
   example by the GUI), but autostart from the host GNOME Shell extension
   needs a host-side activation file or an explicit start. Decide whether
   to start `ferdim-dbus` alongside the GUI.
3. **Extension location.** The package installs the WebExtension inside
   the sandbox only if it is copied there; the current manifest
   deliberately does not, because users install the extension in Firefox
   from AMO. If an offline bundle is wanted, add
   `cp -a extension` and the `zip` build dependency.
4. **Two state roots.** A host install and the Flatpak keep separate
   queues/settings (`~/.local/state/ferdim` vs
   `~/.var/app/io.github.proxy_turkey.FerDim/.local/state/ferdim`). This
   is expected for a sandbox, but should be documented for users (the web
   UI port fallback allows both daemons to run).

## Release maintenance

1. Bump `url` (tag) and `sha256` in the Flathub manifest; recompute with
   `curl -sSL <tag archive> | sha256sum`.
2. Add the new `<release>` to the metainfo (newest first).
3. Keep `packaging/flatpak/io.github.proxy_turkey.FerDim.svg` in sync
   with `scripts/icons/ferdim.svg` (copy, same app-id file name).
4. Keep the module commands in both manifests identical.
5. Run the three checks above; the CI workflow repeats them on the tag.
