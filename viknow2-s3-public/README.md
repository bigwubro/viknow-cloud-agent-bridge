# JuiceFS S3 gateway

Uses the official JuiceFS image `juicedata/mount:ce-v1.2.4`, not `viknow2-app`.
The container only runs `juicefs gateway` (MinIO S3 Gateway compatible).

## Private knowledge objects (app default)

App SDK **authenticated** access:

| Item | Value |
|------|--------|
| Endpoint | `http://<host>:19191` |
| Bucket | `viknow` |
| Addressing | path-style |
| Key | `knowledge/libraries/{library_id}/{asset_id}.<ext>` |
| `asset_path` (index-jobs) | `/data/viknow/{key}` |

Do not PUT to MinIO `:19000`.

Upload stays on `knowledge/libraries/...` only. **No** `public/` copy, **no** anonymous prefix.

### Temporary share links (TTL presigned GET)

When the app must hand a URL to a browser or client, the **application server** calls `generate_presigned_url('get_object', ExpiresIn=ttl)` on the **same private key**. Return `access_url` and `expires_at` to the client. SigV4 presign is **not** permanent (typical max ~7 days). Full flow and boto3 examples: Feishu **§1.6**.

**Delete asset**: `DeleteObject(source_key)` on the app server before dropping DB / calling `DELETE /api/v1/knowledge/indexes` (ViKnow does not remove S3 objects).

## Compose

See `docker-compose.yml` in this directory and the live unit on 236.

## Legacy ops script

`setup-public-anonymous.sh` configured anonymous read on a `public/` prefix. That product pattern is **removed**; do not use for new app integration. Keep only if cleaning up old gateway policy on a host.
