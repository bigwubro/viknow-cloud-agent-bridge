# Storage download grants (index / Actio / 8600)

## Flow

1. Playground catalogs assets on **viknow-storage** (`5219`).
2. `POST /api/v1/knowledge/index-jobs` on platform (`5218`) calls storage `POST .../download-url` (Bearer) and receives a **24h HMAC grant URL**.
3. Platform passes `source_download_url` in HyperRAG job params (also `asset_path` as `storage://{library}/{asset}.ext` for catalog identity).
4. HyperRAG `video_parse` resolves `source_download_url` (or mints from `storage://` when env is set) and Actio sends **`media.type=url`** to **8600**, which HTTP GETs the grant URL.
5. **5175 / S3 object media** for legacy ViKnow2 flows is unchanged.

## Storage env

| Variable | Purpose |
|----------|---------|
| `STORAGE_GRANT_SECRET` | HMAC signing secret |
| `STORAGE_GRANT_TTL_SECONDS` | Default `86400` |
| `STORAGE_DOWNLOAD_BASE_URL` | Public base for grant URLs (must be reachable from HyperRAG workers **and** 8600) |

On 236 Docker, grant URLs often need the bridge gateway (e.g. `http://172.21.0.1:5219`) so `hyperrag-test` and host services can fetch content without hairpin timeouts.

**207 Playground + Actio on 236:** set `STORAGE_DOWNLOAD_BASE_URL=http://36.103.198.236:5220` and run the 236 nginx proxy plus 207→236 SSH reverse tunnel (`deploy/236/storage-proxy-207`, `deploy/207/storage-tunnel-to-236.sh`).

## HyperRAG worker env (fallback mint)

When job params omit `source_download_url`, HyperRAG can mint grants if set:

- `VIKNOW_STORAGE_URL`
- `VIKNOW_STORAGE_TOKEN`

## Actio

Platform in-process Actio must support HTTP grant URLs in `video_parser` / `tasks/segment.py` (8600 `media.type=url`, filename `uploaded.mp4` when path ends with `/content`).
