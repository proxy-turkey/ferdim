# Submitting FerDim to official Fedora repositories

The RPM is verified koji-ready: it builds in a sterile `mock` chroot
(Fedora 43, x86_64), `rpmlint` reports zero errors/warnings, and the full
26-test suite runs inside `%check` at build time. What remains requires a
**Fedora contributor account** and cannot be automated from this repository.

## What you (the submitter) must do — step by step

### 1. Create a Fedora account (FAS)

1. Register at <https://accounts.fedoraproject.org/>.
2. Sign the **Contributor License Agreement (CLA)** in your profile.

### 2. Join the packager group (needs a sponsor)

1. In FAS, request membership of the **packager** group ("Join packager").
2. A sponsor (an existing Fedora packager) must approve you. Ask in
   `#fedora-devel` on Libera.Chat (or the Fedora devel mailing list) —
   mention you have a complete, review-ready package; sponsors usually
   respond quickly to candidates who come prepared.

### 3. Build the SRPM

On any Fedora machine with `rpm-build`:

```bash
git clone https://github.com/proxy-turkey/ferdim.git
cd ferdim
bash scripts/build_srpm.sh
# -> ~/rpmbuild/SRPMS/ferdim-1.3.2-1.fcXX.src.rpm
```

### 4. File the package review request

Open a bug at <https://bugzilla.redhat.com/>:

- Product: **Fedora**, Version: `rawhide`
- Component: `ferdim` (a maintainer creates it from your bug if missing)
- Summary: `Review Request: ferdim - Dynamic self-tuning download manager with Firefox integration`
- Whiteboard: `fedora-review?`

Body template (paste, adjust URLs):

```
Spec URL: https://download.copr.fedorainfracloud.org/.../ferdim.spec
SRPM URL: .../ferdim-1.3.2-1.fcXX.src.rpm
Description: FerDim is a dynamic, self-tuning download manager for Linux:
multi-connection segmented downloads (up to 32 links) with a throughput
governor that calibrates the connection count to saturate the line or the
server; crash-safe resume; graceful adaptation to server throttling;
Firefox WebExtension + native messaging host; CLI/web/GTK front-ends in
four languages. Pure Python (stdlib), MIT license.

Koji scratch build:
https://koji.fedoraproject.org/koji/taskinfo?taskID=...   (optional but recommended)

This is my first Fedora package; I need a sponsor.
fedora-review flag: ?
```

Tip: upload the spec/SRPM anywhere stable — a COPR build or your Fedora
people page work well. Running `koji build --scratch` (after getting packager
access) and linking it earns goodwill.

### 5. The review

A reviewer runs `fedora-review -b <bug>`. Expect requests such as:

- more `%check` coverage (already maximal: full suite),
- a `--no-browser` subpackage split, or dropping the unsigned extension
  bundle from the binary RPM (easy: remove the zip block if asked),
- description nitpicks.

Everything else — license tag, scriptlets, macros, file ownership,
directory layout, man pages — has already been brought to current
guidelines.

### 6. After approval (`fedora-review+`)

With a sponsor's approval you can import into dist-git yourself:

```bash
fedpkg clone ferdim && cd ferdim
fedpkg new-sources ~/rpmbuild/SOURCES/v1.3.2.tar.gz
cp packaging/ferdim.spec ferdim.spec
fedpkg commit -m "Initial import (1.3.2)" && fedpkg push
fedpkg build            # builds in koji for rawhide
fedpkg update           # Bodhi update for branched releases
```

Users then install with plain `dnf install ferdim` after the update goes
stable.

## Why not just COPR?

COPR (`dnf copr enable …`) works today without any review and is the
recommended stop-gap — see [packaging.md](packaging.md). Official repos add
signing, mirroring, and automatic rebuilds against new Python/systemd
releases.

## Reviewer crib-sheet (state of the package)

| Item | State |
|---|---|
| License | MIT, SPDX-valid, license file installed via `%license` |
| BuildArch | noarch; payload at `/usr/share/ferdim` |
| `%check` | full 26-test suite runs during build (mock-verified) |
| rpmlint | 0 errors, 0 warnings (spec, SRPM, RPM) |
| scriptlets | none (systemd user-unit file triggers handle reload) |
| man pages | `ferdim(1)`, `ferdim-nm(1)`, `ferdim-gui(1)` |
| systemd | user unit `ferdim.service` via `%{_userunitdir}` |
| Firefox integration | native-messaging manifest in `/usr/share/mozilla/native-messaging-hosts` |
| Extension bundle | shipped unsigned at `/usr/share/ferdim/ferdim-extension.zip` (AMO self-signing documented) |
