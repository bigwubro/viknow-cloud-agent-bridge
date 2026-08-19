#!/usr/bin/env bash
# Configure git.qingxiang.tech (gateway) to forward :2223 -> this host :2223 for Scheme A.
set -euo pipefail

GATEWAY_PORT="${GATEWAY_FORWARD_PORT:-2223}"
TARGET_PORT="${GATEWAY_TARGET_PORT:-2223}"
GATEWAY_HOSTS="${GATEWAY_HOSTS:-10.168.1.233 101.71.223.113 git.qingxiang.tech}"
GATEWAY_USERS="${GATEWAY_USERS:-root}"
UNIT_NAME="viknow-cursor-agent-ssh-forward.service"

section() { printf '\n========== %s ==========\n' "$*"; }

require_root() {
  [ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
}

local_target_ip() {
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [ -n "${ip}" ]; then
    echo "${ip}"
    return
  fi
  ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}'
}

remote_forward_script() {
  local target_ip="$1"
  cat <<EOS
set -euo pipefail
TARGET_IP='${target_ip}'
GATEWAY_PORT='${GATEWAY_PORT}'
TARGET_PORT='${TARGET_PORT}'
UNIT='/etc/systemd/system/${UNIT_NAME}'
if ! command -v socat >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq socat
fi
cat > "\${UNIT}" <<EOF
[Unit]
Description=Forward cursor-agent SSH port to 236
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/socat TCP-LISTEN:\${GATEWAY_PORT},fork,reuseaddr,bind=0.0.0.0 TCP:\${TARGET_IP}:\${TARGET_PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now "\${UNIT_NAME}"
sleep 1
systemctl is-active "\${UNIT_NAME}"
ss -tlnp | grep -E ":\${GATEWAY_PORT}\\b" || true
echo gateway_forward_ok
EOS
}

try_configure_gateway() {
  local target_ip="$1"
  local script
  script="$(remote_forward_script "${target_ip}")"
  local host user spec
  for host in ${GATEWAY_HOSTS}; do
    for user in ${GATEWAY_USERS}; do
      spec="${user}@${host}"
      section "Try gateway setup via ${spec}"
      if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "${spec}" "bash -s" <<< "${script}" 2>&1 | tee /tmp/gateway-forward.log | grep -q gateway_forward_ok; then
        echo "gateway forward configured via ${spec} -> ${target_ip}:${TARGET_PORT}"
        return 0
      fi
    done
  done
  return 1
}

show_status() {
  section "236 listeners on ${TARGET_PORT}"
  ss -tlnp | grep -E ":${TARGET_PORT}\\b" || echo "not listening on 236"
  section "Probe gateway ${GATEWAY_PORT} banner"
  python3 - <<'PY' 2>/dev/null || true
import socket
for host in ("101.71.223.113", "git.qingxiang.tech"):
    s = socket.socket()
    s.settimeout(4)
    try:
        s.connect((host, 2223))
        print(host, repr(s.recv(120)))
    except Exception as e:
        print(host, "ERR", e)
    finally:
        s.close()
PY
}

install_all() {
  local target_ip
  target_ip="$(local_target_ip)"
  section "Local target IP"
  echo "${target_ip}"
  hostname
  if try_configure_gateway "${target_ip}"; then
    section "Success"
    echo "Cloud Agent: ssh -p ${GATEWAY_PORT} cursor-agent@git.qingxiang.tech"
  else
    section "Gateway auto-config failed"
    echo "236 sshd on :${TARGET_PORT} is ready, but gateway (${GATEWAY_HOSTS}) could not be configured automatically."
    echo "Manual step on 101.71.223.113:"
    echo "  socat TCP-LISTEN:${GATEWAY_PORT},fork,reuseaddr TCP:${target_ip}:${TARGET_PORT}"
    exit 1
  fi
}

require_root
case "${GATEWAY_FORWARD_ACTION:-install}" in
  install) install_all ;;
  status) show_status ;;
  *)
    echo "unknown GATEWAY_FORWARD_ACTION" >&2
    exit 1
    ;;
esac
section "Done"
