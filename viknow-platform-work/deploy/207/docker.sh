#!/usr/bin/env bash
# Use sudo when the login user cannot access docker.sock (207 default).
set -euo pipefail
if docker info >/dev/null 2>&1; then
  exec docker "$@"
fi
if sudo docker info >/dev/null 2>&1; then
  exec sudo docker "$@"
fi
echo "docker unavailable (tried user and sudo)" >&2
exit 1
