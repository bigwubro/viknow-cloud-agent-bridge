#!/usr/bin/env bash
# Run one command on 236 over SSH ControlMaster (see AGENTS.md).
set -euo pipefail
if [[ $# -lt 1 ]]; then
  echo "usage: $0 <remote command...>" >&2
  exit 2
fi
exec ssh viknow236 "$@"
