#!/usr/bin/env bash
# Start a per-engine LMCache MP server, then exec vLLM. Used as container entrypoint.
set -euo pipefail
export LMCACHE_DISABLE_BANNER=1
LMC_ZMQ_PORT="${LMC_ZMQ_PORT:?}"
LMC_HTTP_PORT="${LMC_HTTP_PORT:?}"
LMC_INSTANCE_ID="${LMC_INSTANCE_ID:?}"
LMC_L1_GB="${LMC_L1_GB:-16}"
LMC_CHUNK="${LMC_CHUNK:-2112}"

lmc_args=(
  server
  --instance-id "${LMC_INSTANCE_ID}"
  --host 127.0.0.1
  --port "${LMC_ZMQ_PORT}"
  --http-host 127.0.0.1
  --http-port "${LMC_HTTP_PORT}"
  --chunk-size "${LMC_CHUNK}"
  --separate-object-groups
  --l1-size-gb "${LMC_L1_GB}"
  --l1-init-size-gb 2
  --eviction-policy LRU
  --supported-transfer-mode lmcache_driven
  --disable-metrics
)
if [[ -n "${LMC_COORD_URL:-}" && -n "${LMC_P2P_URL:-}" ]]; then
  lmc_args+=(
    --coordinator-url "${LMC_COORD_URL}"
    --p2p-advertise-url "${LMC_P2P_URL}"
    --p2p-listen-url "${LMC_P2P_URL}"
  )
fi

lmcache "${lmc_args[@]}" >/tmp/lmcache-"${LMC_INSTANCE_ID}".log 2>&1 &
echo $! >/tmp/lmcache-"${LMC_INSTANCE_ID}".pid

ok=0
for _ in $(seq 1 80); do
  if curl -sf -m 1 "http://127.0.0.1:${LMC_HTTP_PORT}/status" >/dev/null \
    || curl -sf -m 1 "http://127.0.0.1:${LMC_HTTP_PORT}/" >/dev/null; then
    ok=1
    break
  fi
  sleep 0.25
done
if [[ "$ok" -ne 1 ]]; then
  echo "lmcache server ${LMC_INSTANCE_ID} did not become ready on :${LMC_HTTP_PORT}" >&2
  tail -n 80 /tmp/lmcache-"${LMC_INSTANCE_ID}".log >&2 || true
  exit 1
fi

exec vllm serve "$@"
