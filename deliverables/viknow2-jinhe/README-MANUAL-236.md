# 236 手工冒烟（与交付 tar 同目录）

目录：**`/home/cursor-agent/nas-staging/viknow-deploy-image/`**

| 文件 | 用途 |
|------|------|
| `viknow2-app-56d8705e5feb.tar` | 交付镜像 |
| `docker-compose.yml` | 客户现场版（external 网络，无 extra_hosts） |
| `docker-compose.236-smoke.yml` | **236 推荐**：与客户 compose 同结构（external data 网络） |
| `deploy.on-site-minimal.env` | 客户现场模板（CHANGE_ME） |
| `deploy.env.236` | 236 已填好中间件 + 模型（复制为 deploy.env） |
| `data/` | 业务数据目录（compose 挂载 `./data`） |

## 236 上快速试启动

```bash
cd /home/cursor-agent/nas-staging/viknow-deploy-image

# 1. 加载镜像（只需一次）
docker load -i viknow2-app-56d8705e5feb.tar

# 2. 确认 test-data 栈在跑（15432/16379/19000/17687）
docker ps --format '{{.Names}}\t{{.Ports}}' | grep viknow-test-data

# 3. 准备 env
cp deploy.env.236 deploy.env
chmod 600 deploy.env

# 4. 启动（端口 8006，容器名 viknow2-jinhe-manual）
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
| compose | `docker-compose.236-smoke.yml`（无 extra_hosts） | `docker-compose.yml` |
| deploy.env | `deploy.env.236` | `deploy.on-site-minimal.env` |
| 中间件 / Actio | `10.200.0.1` + test 映射端口 | 客户内网 IP |
| 模型 | `172.30.57.94` | 客户 LiteLLM IP |
| 监控 | `127.0.0.1` 占位 | `127.0.0.1` 占位 |
