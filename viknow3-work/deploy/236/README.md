# Playground container deploy (236)

Manual Gitea workflow **Playground 236** (`workflow_dispatch` on `main`):

1. Builds `viknow3-playground:<12-char-sha>` on runner `python-312-uv` (same host as 236 services).
2. Stops host `scripts/deploy-playground-ui.sh` processes.
3. Runs `viknow3-playground` with `--network host`, mounting `/data/viknow`, state dir, and `docker.sock` (MinerU wrapper).

## Config layout (方案 2)

**规范全文：`deploy/236/env/ENV.md`**

| File | Purpose |
|------|---------|
| `env/ui.env` | **Playground UI only**: `VIKNOW_UI_*`, `VIKNOW_PLAYGROUND_API_URL` |
| `env/api.env` | **ViKnow API**: ports, CORS, storage, data paths, HyperRAG/Actio/MinerU |
| `env/secrets.env` | DSNs, tokens, `VIKNOW3_APP_KEY` — **private Gitea only** |
| `env/*.example` | Templates without secrets |

`docker run` loads **`api.env` → `secrets.env` → `ui.env`**.

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
