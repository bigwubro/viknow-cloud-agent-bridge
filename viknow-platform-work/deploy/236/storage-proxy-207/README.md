# 236 → 207 storage 反代（Actio grant URL）

Actio 在 **236** 上处理视频索引时会 **HTTP GET** platform 下发的 `source_download_url`。207 上 `STORAGE_DOWNLOAD_BASE_URL` 不能填内网 `10.168.1.207:5219`（236 不可达），应填 **236 上的反代入口**。

## 拓扑

```text
Actio (236)  GET  http://36.103.198.236:5220/libraries/.../content?grant=...
                    │
                    ▼
         nginx (viknow236-storage-proxy-207)
                    │
                    ▼
         127.0.0.1:15219  ←── SSH -R from 207
                    │
                    ▼
         207 viknow-storage :5219
```

## 236 安装

```bash
bash deploy/236/storage-proxy-207/install-on-236.sh
curl -sf http://127.0.0.1:5220/health   # 需 207 隧道已建立
curl -sf http://36.103.198.236:5220/health
```

## 207 隧道（207 上执行，需 SSH 公钥已加入 236 `cursor-agent`）

```bash
bash deploy/207/storage-tunnel-to-236.sh
# 或 systemd: deploy/207/systemd/viknow207-storage-tunnel.service.example
```

## 207 storage 配置

在 `~/viknow207.host.env`：

```bash
STORAGE_DOWNLOAD_BASE_URL=http://36.103.198.236:5220
```

然后 `docker compose … up -d storage` 或跑 `deploy-207.sh`。

Playground 浏览器仍可用 `ui.env` 里的 `http://10.168.1.207:5219`；仅 **grant URL 前缀** 走 236 反代。

本地 smoke（无 207 隧道时）：`STORAGE_PROXY_UPSTREAM=127.0.0.1:5219 install-on-236.sh` 可验证 nginx 本身（指向 236 本地 storage）。
