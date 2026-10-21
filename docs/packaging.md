# Packaging FerDim for Fedora

FerDim is a noarch Python package plus a Firefox WebExtension. This document
covers the three publication tiers and what each requires.

## 1) COPR — the fast track (`dnf copr enable … && dnf install ferdim`)

COPR is Fedora's personal build service and the quickest way to make
`dnf install ferdim` work for everyone.

Prerequisites: a [Fedora account](https://accounts.fedoraproject.org/) (FAS)
and `dnf install copr-cli fedora-packager`.

```bash
# 0) srpm uret (script tarbali hazirlar, spec ile srpm derler)
bash scripts/build_srpm.sh

# 1) copr projesi ac (bir kez)
copr-cli create ferdim \
  --chroot fedora-41-x86_64 --chroot fedora-42-x86_64 \
  --chroot fedora-rawhide-x86_64 \
  --description "Dynamic self-tuning download manager with Firefox integration"

# 2) srpm'i yukle
copr-cli build ferdim ~/rpmbuild/SRPMS/ferdim-*.src.rpm

# 3) kullanicilar icin kurulum:
#    dnf copr enable <kullanici-adi>/ferdim && dnf install ferdim
```

`scripts/build_srpm.sh`:

```bash
#!/bin/bash
set -euo pipefail
VER="$(python3 -c 'import sys; sys.path.insert(0,"ferdim"); from ferdim import __version__; print(__version__)')"
mkdir -p ~/rpmbuild/SOURCES
tar czf ~/rpmbuild/SOURCES/ferdim-$VER.tar.gz --transform "s,^ferdim,ferdim-$VER," \
    --exclude='__pycache__' --exclude='.git' ferdim
rpmbuild -bs packaging/ferdim.spec
echo "SRPM: $(ls -t ~/rpmbuild/SRPMS/*.src.rpm | head -1)"
```

## 2) Official Fedora repositories

The full process (summarized from the
[Fedora packaging guidelines](https://docs.fedoraproject.org/en-US/packaging-guidelines/)):

1. Join the *packager* group — you need a sponsor (see
   `Join the package maintainers` in Fedora docs).
2. Run `fedora-review` on your package until it is clean; fix every warning
   in `packaging/ferdim.spec`.
3. File a **Review Request** (`fedora-review` bug) in Fedora Bugzilla under
   `Fedora / ferdim`.
4. Once approved (`fedora-review+`), import into `fedora-dist-git` via
   `fedpkg`, build with `fedpkg build`, and submit the Bodhi update.
5. After a few releases in Fedora proper, FerDim can be proposed for EPEL.

Typical polish items the reviewer will ask for (already done):

- noarch package, `%license`, verbatim upstream tarball (`Source0` tag URL),
- systemd user unit via `%{_userunitdir}` + `%systemd_user_post`,
- `Requires: python3`, soft deps for the GTK front-end and Firefox,
- no bundled vendored code, MIT license file included.

## 3) Release tarballs on GitHub

Tagging is enough — the spec downloads the tag archive directly:

```bash
git tag -s v1.3.0 -m "FerDim 1.3.0"
git push origin v1.3.0
```

`Source0: https://github.com/proxy-turkey/ferdim/archive/refs/tags/v%{version}.tar.gz`
resolves automatically (GitHub archives strip the leading `v`, which the
spec handles by default since the directory is `ferdim-<version>`).

## Extension signing

The WebExtension must be signed for permanent installation in release
Firefox. The usual flow is self-service AMO unlisted signing with
`web-ext sign` (see README → Firefox integration). Distributors do not need
to sign anything themselves; users do it once with a free account.
