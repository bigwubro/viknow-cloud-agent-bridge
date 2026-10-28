# ViKnow Playground UI

OpenAI Platform–style shell for ViKnow 3.0. The first slice is **user create + login** against the same Postgres as the access layer (`VIKNOW_ACCESS_DSN`).

## Run locally

From the repository root:

```bash
export PYTHONPATH="$(pwd)/src:$(pwd)"
export VIKNOW_ACCESS_DSN='postgresql://…/viknow3_access'
# optional: load DSN from a file
export VIKNOW3_ENV_FILE=/path/to/runtime.env

python -m ui.server
```

Open `http://127.0.0.1:5198/playground`.

Environment:

| Variable | Default | Purpose |
|----------|---------|---------|
| `VIKNOW_ACCESS_DSN` | (required) | Access DB for `users` table |
| `VIKNOW3_ENV_FILE` | — | `KEY=value` lines merged into env before user service init |
| `VIKNOW_UI_HOST` | `127.0.0.1` | Bind address |
| `VIKNOW_UI_PORT` | `5198` | HTTP port |

## API (BFF)

- `POST /ui/playground/users` — `{ "display_name": "…" }` optional
- `POST /ui/playground/login` — `{ "user_id": "…" }` validates user exists
- `GET /ui/playground/users/{user_id}` — same as login lookup

Browser session state is `localStorage` key `viknow_playground_user` (not a server session cookie).
