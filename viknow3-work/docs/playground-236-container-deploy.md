# Playground container deploy (236)

Manual Gitea workflow **Playground 236** (`workflow_dispatch` on `main`):

1. Builds `viknow3-playground:<12-char-sha>` on runner `python-312-uv` (same host as 236 services).
2. Stops host `scripts/deploy-playground-ui.sh` processes.
3. Runs `viknow3-playground` with `--network host`, mounting `/data/viknow`, state dir, and `docker.sock` (MinerU wrapper).

## Config layout

| File | Purpose |
|------|---------|
| `env/playground.env` | Ports, public URLs, data paths (no secrets) |
| `env/secrets.env` | DSNs, API keys, storage token — **private Gitea only** |
| `env/secrets.env.example` | Key names only for onboarding |

Refresh `secrets.env` from 236 host files:

```bash
bash deploy/236/env/refresh-secrets-from-host.sh
```

## Local deploy (on 236)

```bash
docker build -f deploy/236/Dockerfile -t viknow3-playground:local .
bash deploy/236/deploy.sh viknow3-playground:local
```

Rollback to host processes:

```bash
docker rm -f viknow3-playground
bash scripts/deploy-playground-ui.sh restart
```
