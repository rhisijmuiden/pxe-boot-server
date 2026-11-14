#!/usr/bin/env bash
# Builds the iPXE loaders with the embedded script. Used by CI and by install.sh --build-ipxe.
# Usage: ipxe/build.sh <output-dir> [ipxe-git-ref]
set -euo pipefail
OUT="$(realpath -m "${1:?output dir}")"
REF="${2:-master}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git clone --depth 1 --branch "$REF" https://github.com/ipxe/ipxe.git "$WORK/ipxe" >/dev/null 2>&1 || \
  git clone --depth 1 https://github.com/ipxe/ipxe.git "$WORK/ipxe"
cp "$HERE/general.h" "$WORK/ipxe/src/config/local/general.h"
cd "$WORK/ipxe/src"
JOBS="$(nproc 2>/dev/null || echo 2)"
make -j"$JOBS" bin/undionly.kpxe EMBED="$HERE/embed.ipxe" NO_WERROR=1 >/dev/null
make -j"$JOBS" bin-x86_64-efi/ipxe.efi bin-x86_64-efi/snponly.efi EMBED="$HERE/embed.ipxe" NO_WERROR=1 >/dev/null
mkdir -p "$OUT"
cp bin/undionly.kpxe bin-x86_64-efi/ipxe.efi bin-x86_64-efi/snponly.efi "$OUT/"
git rev-parse --short HEAD > "$OUT/IPXE_VERSION"
echo "iPXE $(cat "$OUT/IPXE_VERSION") built into $OUT"
