#!/bin/bash
# FerDim kaldirici. Verileri de silmek icin: --purge
exec bash "$(dirname "$(readlink -f "$0")")/install.sh" --uninstall "${1:-}"
