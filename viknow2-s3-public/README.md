# JuiceFS S3 gateway

Uses the official JuiceFS image `juicedata/mount:ce-v1.2.4`, not `viknow2-app`.
The container only runs `juicefs gateway` (MinIO S3 Gateway compatible).

## Private knowledge objects (default)

App SDK **authenticated** access:

| Item | Value |
|------|--------|
| Endpoint | `http://<host>:19191` |
| Bucket | `viknow` |
| Addressing | path-style |
| Key | `knowledge/libraries/{library_id}/{asset_id}.<ext>` |
| `asset_path` (index-jobs) | `/data/viknow/{key}` |

Do not PUT to MinIO `:19000`.

Keep uploads on `knowledge/libraries/...` as today. For a fixed public URL, **after** a successful upload run `CopyObject` to `public/{scope}/{publish_id}/{filename}` and build `public_url` (see Feishu appendix B.3).

## Public static objects (`public/` prefix)

For **fixed, unsigned URLs** (like OSS public-read on a prefix):

| Item | Value |
|------|--------|
| Key | `public/{scope}/{publish_id}/{filename}` |
| Example scope | `library_id` or tenant id |
| `publish_id` | UUID / random id (avoid guessable paths) |
| Public URL (path-style) | `http://<host>:19191/viknow/public/{scope}/{publish_id}/{filename}` |

Rules:

- Only objects under `public/` may be read **without** Access Key.
- `knowledge/libraries/**` stays **private** (403 without SigV4).
- Upload still uses **PutObject with AK/SK**; only **GetObject** is anonymous for `public/`.
- Do not rely on anonymous **ListBucket** (do not publish directory listings).

### One-time / after gateway recreate

On the gateway host (236 unit: `/home/cursor-agent/services/viknow-juicefs-s3-gateway`):

```bash
set -a && source .env && set +a
GATEWAY_URL=http://127.0.0.1:19191 bash /path/to/setup-public-anonymous.sh
```

This runs `mc anonymous set download` on `viknow/public/` (via `minio/mc` container).

Verify:

```bash
curl -s "http://<host>:19191/viknow/public/<scope>/<id>/<file>"   # 200 without Authorization
curl -s -o /dev/null -w "%{http_code}\n" \
  "http://<host>:19191/viknow/knowledge/libraries/..."            # 403
```

Production should expose **`https://<domain>/viknow/public/...`** (reverse proxy on 443), not raw `:19191` to end users.

## Compose

See `docker-compose.yml` in this directory and the live unit on 236.
