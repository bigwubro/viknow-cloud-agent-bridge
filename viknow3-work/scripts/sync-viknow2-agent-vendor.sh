#!/usr/bin/env bash
# Refresh vendored viknow2 agent code (as-is migration). Run from viknow3 repo root.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VIKNOW2_SRC="${VIKNOW2_SRC:-/home/cursor-agent/work/viknow2}"
REV="${VIKNOW2_REV:-main}"

if [[ ! -d "$VIKNOW2_SRC/.git" ]]; then
  echo "VIKNOW2_SRC must point at a viknow2 checkout: $VIKNOW2_SRC" >&2
  exit 1
fi

(
  cd "$VIKNOW2_SRC"
  git fetch origin "$REV"
  git checkout "origin/$REV" -- src/viknow config/profiles
)

rsync -a --delete "$VIKNOW2_SRC/src/viknow/" "$ROOT/src/viknow/"
rsync -a "$VIKNOW2_SRC/config/profiles/" "$ROOT/config/profiles/"

echo "Synced viknow package from $VIKNOW2_SRC @ origin/$REV"
echo "  src/viknow: $(find "$ROOT/src/viknow" -name '*.py' | wc -l) Python files"
echo "Next: apply boundary patches (HyperRAG/Actio/grant) per docs/viknow-agent-vendor.md"
