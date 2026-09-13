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

# 3. 启动（compose 直接读 deploy.env.236，无需 cp）
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

### 若 `docker rm -f` 报 zombie PID 删不掉

镜像内 JuiceFS 会 fork 子进程；未启用 `init` 时主进程异常退出后，容器 init PID 可能变 zombie，Docker 无法 kill。

**优先**用 compose 重建（已启用 `init: true`）：

```bash
docker compose -f docker-compose.236-smoke.yml down --remove-orphans
docker compose -f docker-compose.236-smoke.yml up -d --force-recreate
```

仍卡住时，在宿主机上先杀容器命名空间内子进程，再删容器：

```bash
CID=viknow2-jinhe-manual
docker top "$CID" -eo pid | tail -n +2 | xargs -r sudo kill -9
sudo kill -9 "$(docker inspect -f '{{.State.Pid}}' "$CID")" 2>/dev/null || true
docker rm -f "$CID"
```

最后手段：`sudo systemctl restart docker`（会短暂影响本机其它容器）。

## 与客户现场差异

| 项 | 236 冒烟 | 客户现场 |
|----|----------|----------|
| compose | `docker-compose.236-smoke.yml`（`extra_hosts` 只给 JuiceFS 解析 `host.docker.internal`） | `docker-compose.yml` |
| deploy.env | `deploy.env.236` | `deploy.on-site-minimal.env` |
| JuiceFS 对象存储 | 元数据已写成 `host.docker.internal:19000`，必须 `extra_hosts` | 现场 format 时写成客户 MinIO 地址 |
| 中间件 / Actio | `10.200.0.1` + test 映射端口 | 客户内网 IP |
| 模型 | `172.30.57.94` | 客户 LiteLLM IP |
| 监控 | `127.0.0.1` 占位 | `127.0.0.1` 占位 |
