#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMG_DIR="${ROOT}/images"
if [[ ! -d "$IMG_DIR" ]]; then
  echo "missing $IMG_DIR — create and place *.tar from MANIFEST.yaml"
  exit 1
fi
shopt -s nullglob
for tar in "$IMG_DIR"/*.tar "$IMG_DIR"/*.tar.gz; do
  echo "==> loading $tar"
  case "$tar" in
    *.tar.gz) gunzip -c "$tar" | docker load ;;
    *) docker load -i "$tar" ;;
  esac
done
echo "done. docker images | head"
