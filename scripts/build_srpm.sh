#!/bin/bash
# ferdim icin kaynak RPM (srpm) uretir — COPR derlemeleri icin.
# Tarball, spec'in Source0 (GitHub v<VER> tag arsivi) yapisina uygun olarak
# "ferdim-<VER>/" kok diziniyle repo kokunden olusturulur.
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
VER="$(cd "$REPO" && python3 -c 'import sys; sys.path.insert(0,"ferdim"); from ferdim import __version__; print(__version__)')"
mkdir -p ~/rpmbuild/SOURCES
tar czf ~/rpmbuild/SOURCES/v$VER.tar.gz -C "$REPO/.." \
    --transform "s,^ferdim\(/\|$\),ferdim-$VER\1," \
    --exclude='__pycache__' --exclude='.git' --exclude='*.zip' ferdim
rpmbuild -bs "$REPO/packaging/ferdim.spec"
echo "SRPM hazir: $(ls -t ~/rpmbuild/SRPMS/ferdim-*.src.rpm | head -1)"
