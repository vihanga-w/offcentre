#!/bin/sh
# Render the social preview SVG to PNG with headless Chrome (a throwaway profile, not yours).
# Usage: sh assets/brand/render.sh
set -e
cd "$(dirname "$0")/.."
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
PROFILE="$(mktemp -d)"
trap 'rm -rf "$PROFILE"' EXIT
OUT="$PWD/social-preview.png"
rm -f "$OUT"
# Headless Chrome sometimes lingers after writing the screenshot, so wait for the file and stop it.
"$CHROME" --headless=new --disable-gpu --hide-scrollbars --no-first-run --user-data-dir="$PROFILE" \
  --default-background-color=ffffffff --window-size=1280,640 \
  --screenshot="$OUT" "file://$PWD/social-preview.svg" >/dev/null 2>&1 &
PID=$!
i=0
while [ ! -s "$OUT" ] && [ $i -lt 60 ]; do sleep 0.5; i=$((i + 1)); done
sleep 0.5
kill "$PID" 2>/dev/null || true
[ -s "$OUT" ] && echo "wrote assets/social-preview.png" || { echo "render failed" >&2; exit 1; }
