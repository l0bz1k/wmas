#!/bin/bash
# Build bin/ipatool: upstream ipatool at the pinned commit plus the WMAS patches.
# Requires git and Go. Set GOPROXY=direct GOSUMDB=off if proxy.golang.org is
# unreachable from your network.
set -euo pipefail

ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FORK="$ROOT/ipatool-fork"
SRC="${WMAS_IPATOOL_SRC:-$FORK/src}"
OUT="${WMAS_IPATOOL_OUT:-$ROOT/bin/ipatool}"
COMMIT="$(awk '$1 == "commit:" { print $2 }' "$FORK/UPSTREAM")"

command -v go >/dev/null || { echo "Go is required: https://go.dev/dl/ or 'brew install go'" >&2; exit 1; }

if [ ! -d "$SRC/.git" ]; then
  git clone https://github.com/majd/ipatool.git "$SRC"
fi

cd "$SRC"
git fetch --quiet origin "$COMMIT" 2>/dev/null || git fetch --quiet origin
git checkout --quiet --force "$COMMIT"
git clean --quiet -fd
cp "$FORK/storefront_static.go" pkg/appstore/
git apply "$FORK/storefront-fix.patch" "$FORK/version-probe.patch" "$FORK/profiles.patch"

mkdir -p "$(dirname "$OUT")"
go build -o "$OUT" .
echo "built $OUT"
