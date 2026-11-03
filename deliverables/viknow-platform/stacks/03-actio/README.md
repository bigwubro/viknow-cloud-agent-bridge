# Actio 栈（可选）

Actio 镜像与 compose 随 **viknow2 / actio 仓库** 版本变化，不在跳板仓固化。

**Platform 发包时：**

1. 从 236 现网或 release 目录取 `docker/compose.video-split.yml`（或客户已在用的 Actio compose）。
2. 放入本目录 `docker-compose.yml`，并保证：
   - `networks` 加入 **`viknow-platform_data`**（external）。
   - 容器内监听端口与 `deploy.env` 中 `ACTIO_SERVER_URL` 一致（例如 `http://actio:8080`）。
3. 若不部署 Actio，使用 [`viknow2-jinhe/deploy.on-site-minimal.env`](../../viknow2-jinhe/deploy.on-site-minimal.env) 中的占位 `ACTIO_*`，视频切分/部分 multimodal 能力不可用。
