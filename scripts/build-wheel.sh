#!/usr/bin/env bash
# Build the python wheel containing BOTH:
#   - the rust `_west_native` PyO3 extension (cdylib, in `west/`)
#   - the rust `west` CLI binary (in `west-VERSION.data/scripts/`)
#
# Maturin's `bindings = "pyo3"` mode ships the cdylib but doesn't auto-
# build/include `[[bin]]` targets (open issue PyO3/maturin#368). The
# workaround: pre-build the CLI binary, stage it under `.wheel-data/
# scripts/`, and let maturin pick it up via `[tool.maturin] data`.
#
# Forwarded args go to maturin — typically `build` (default) or
# `develop` (editable install into the active venv).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROFILE="release"
EXT=""
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) EXT=".exe" ;;
esac

echo ">>> building CLI binary (--features pyo3 picks up the cdylib too)"
cargo build --"$PROFILE" --features pyo3 -p west-cli

echo ">>> staging binary under .wheel-data/scripts/"
mkdir -p .wheel-data/scripts
cp "target/$PROFILE/west$EXT" ".wheel-data/scripts/west$EXT"

ACTION="${1:-build}"
shift || true
echo ">>> running maturin $ACTION $*"
exec python -m maturin "$ACTION" "$@"
