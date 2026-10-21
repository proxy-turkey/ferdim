#!/bin/bash
# ferdim icin kaynak RPM (srpm) uretir — COPR derlemeleri icin.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
VER="$(python3 -c 'import sys; sys.path.insert(0,"ferdim"); from ferdim import __version__; print(__version__)')"
mkdir -p ~/rpmbuild/SOURCES
tar czf ~/rpmbuild/SOURCES/ferdim-$VER.tar.gz \
    --transform "s,^ferdim,ferdim-$VER," \
    --exclude='__pycache__' --exclude='.git' --exclude='*.zip' ferdim
rpmbuild -bs packaging/ferdim.spec
echo "SRPM hazir: $(ls -t ~/rpmbuild/SRPMS/ferdim-*.src.rpm | head -1)"
