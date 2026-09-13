# 236 手工冒烟（与交付 tar 同目录）

目录：**`/home/cursor-agent/nas-staging/viknow-deploy-image/`**

| 文件 | 用途 |
|------|------|
| `viknow2-app-56d8705e5feb.tar` | 交付镜像 |
| `docker-compose.yml` | 客户现场版（external 网络，无 extra_hosts） |
| `docker-compose.236-smoke.yml` | **236 推荐**：加 extra_hosts，连已有模型 |
| `deploy.on-site-minimal.env` | 客户现场模板（CHANGE_ME） |
| `deploy.env.236` | 236 已填好中间件 + 模型（复制为 deploy.env） |
| `data/` | 业务数据目录（compose 挂载 `./data`） |

## 236 上快速试启动

```bash
cd /home/cursor-agent/nas-staging/viknow-deploy-image

# 1. 加载镜像（只需一次）
docker load -i viknow2-app-56d8705e5feb.tar

# 2. 确认 data 栈在跑（四件套）
docker ps --format '{{.Names}}' | grep viknow-private-data

# 3. 准备 env
cp deploy.env.236 deploy.env
chmod 600 deploy.env

# 4. 启动（端口 8006，容器名 viknow2-jinhe-manual）
export VIKNOW_DATA_NETWORK=viknow-private-data_appnet
export VIKNOW_IMAGE=viknow2-app:56d8705e5feb
export VIKNOW_HOST_PORT=8006
export VIKNOW_CONTAINER_NAME=viknow2-jinhe-manual
export VIKNOW_STATE_VOLUME=viknow_jinhe_manual_state

docker compose -f docker-compose.236-smoke.yml up -d

# 5. 看日志 / 验证
docker logs -f viknow2-jinhe-manual
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8006/docs
```

## 停止 / 清理

```bash
cd /home/cursor-agent/nas-staging/viknow-deploy-image
docker compose -f docker-compose.236-smoke.yml down
# 清 state 卷（可选）：docker compose -f docker-compose.236-smoke.yml down -v
```

## 与客户现场差异

| 项 | 236 冒烟 | 客户现场 |
|----|----------|----------|
| compose | `docker-compose.236-smoke.yml` + extra_hosts | `docker-compose.yml` |
| deploy.env | `deploy.env.236` | `deploy.on-site-minimal.env` → 改 CHANGE_ME |
| 中间件 HOST | `viknow-private-data-*-1` | `postgres` / `redis` / …（compose services 名） |
| 网络 | `viknow-private-data_appnet` | 客户 `VIKNOW_DATA_NETWORK` |
