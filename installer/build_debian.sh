#!/usr/bin/env bash
# Builds dist/rdr-debian-<version>.tar.gz: the app plus installer/install_debian.sh
# and the Dockerfile. Run from anywhere on Linux or macOS:
#     bash installer/build_debian.sh
# RDR_DIST picks another output folder.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' app/__init__.py)"
[ -n "$VERSION" ] || { echo "!! No version in app/__init__.py" >&2; exit 1; }
DIST="${RDR_DIST:-dist}"
NAME="rdr-debian-$VERSION"
OUT="$DIST/$NAME"

rm -rf "$OUT" "$DIST/$NAME.tar.gz"
mkdir -p "$OUT/installer"
cp -r app "$OUT/app"
find "$OUT/app" -type d -name __pycache__ -prune -exec rm -rf {} +
cp run.py requirements.txt README.md Dockerfile .dockerignore "$OUT/"
cp installer/install_debian.sh "$OUT/installer/"
chmod 755 "$OUT/installer/install_debian.sh"
printf '%s' "$VERSION" > "$OUT/VERSION"

tar -C "$DIST" --owner=0 --group=0 --numeric-owner -czf "$DIST/$NAME.tar.gz" "$NAME"
rm -rf "$OUT"
echo "== Built $DIST/$NAME.tar.gz"
