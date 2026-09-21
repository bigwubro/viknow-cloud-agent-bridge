#!/usr/bin/env bash
# 订阅 docker events，记录 destroy/kill/die/start 等（无 UID，作 auditd 补充）
set -euo pipefail

LOG_FILE="${1:-${HOME}/.local/log/viknow-docker-events.log}"
mkdir -p "$(dirname "$LOG_FILE")"

log() {
  printf '%s %s\n' "$(date -Is)" "$*" >> "$LOG_FILE"
}

log "watcher_start pid=$$ user=$(id -un) uid=$(id -u)"

# 关注生产相关容器名；可按需扩展
CONTAINER_FILTER='name=vllm-vlm|name=vllm-|name=viknow'

docker events \
  --filter 'type=container' \
  --format '{{.Time}} event={{.Action}} container={{.Actor.Attributes.name}} id={{.Actor.ID}} image={{.Actor.Attributes.image}}' \
  2>>"$LOG_FILE" | while IFS= read -r line; do
    if echo "$line" | grep -qE "$CONTAINER_FILTER"; then
      log "$line"
    fi
  done
