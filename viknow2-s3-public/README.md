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

Upload stays on `knowledge/libraries/...`. **Upload = public**: right after each successful `PutObject`, `CopyObject` to `public/{library_id}/{publish_id}/{asset_id}.{ext}` and return `public_url` in the upload response (Feishu appendix B). Persist `public_key` in your DB.

**Delete asset**: in the same request, `DeleteObject(public_key)` then `DeleteObject(source_key)` before dropping DB / calling `DELETE /api/v1/knowledge/indexes` (ViKnow does not remove S3 objects). On re-upload with the same `asset_id`, delete the previous `public_key` before copying a new one.

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
