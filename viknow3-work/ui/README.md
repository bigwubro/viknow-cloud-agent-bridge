# ViKnow Playground UI

Static Playground shell on **5198**. All business calls go to the **ViKnow access API** (`python -m viknow3`, default **5218**) via `VIKNOW_PLAYGROUND_API_URL` injected as `/playground/assets/runtime-config.js`.

## Run locally

Terminal 1 — access API (needs DSN, HyperRAG, agent deps):

```bash
export PYTHONPATH="$(pwd)/src:$(pwd)"
export VIKNOW_ACCESS_DSN='postgresql://…/viknow_access'
export VIKNOW3_ENV_FILE=/path/to/runtime.env
export VIKNOW_PORT=5218
python -m viknow3
```

Terminal 2 — UI only:

```bash
export PYTHONPATH="$(pwd)/src:$(pwd)"
export VIKNOW_PLAYGROUND_API_URL=http://127.0.0.1:5218
export VIKNOW3_APP_KEY=dev-app-key   # optional; injected into runtime-config for X-App-Key
python -m ui.server
```

Open `http://127.0.0.1:5198/playground`.

On 236 host: `bash scripts/deploy-playground-ui.sh restart` starts API then UI.

| Variable | Default | Purpose |
|----------|---------|---------|
| `VIKNOW_PLAYGROUND_API_URL` | `http://127.0.0.1:5218` | Browser API base (runtime-config.js) |
| `VIKNOW3_APP_KEY` | — | Optional default `X-App-Key` for Playground |
| `VIKNOW_UI_HOST` | `0.0.0.0` | UI bind address |
| `VIKNOW_UI_PORT` | `5198` | UI HTTP port |

Browser session state is `localStorage` key `viknow_playground_user` (not a server session cookie).

Legacy `ui/playground_access.py` (mounting `/api/v1` on the UI process) is deprecated; do not use it for new deployments.
