#!/bin/sh
# Job Radar installer for macOS and Linux:
#   curl -LsSf https://raw.githubusercontent.com/piotr-malek/jobradar-eu/main/install.sh | sh
# Installs uv (Astral's Python tool manager, which brings its own Python) if it's missing, then Job Radar,
# then opens the setup wizard in your browser. Nothing needs admin rights.
set -eu
SPEC="${JOBRADAR_SPEC:-jobradar-eu[full]}"

if ! command -v uv >/dev/null 2>&1; then
  echo "==> Installing uv (it installs Python for Job Radar)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi

echo "==> Installing Job Radar…"
uv tool install --upgrade --python 3.13 "$SPEC"
uv tool update-shell >/dev/null 2>&1 || true

JR="$(uv tool dir --bin)/jr"
echo "==> Opening the setup in your browser. Keep this window open while you use Job Radar."
JOBRADAR_SPEC="${JOBRADAR_SPEC:-}" exec "$JR" setup
