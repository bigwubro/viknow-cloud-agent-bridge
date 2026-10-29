# 207 单机栈（**容器化**）：Postgres / Redis / Neo4j + **viknow-storage**（内置 MinIO）+ **Playground 镜像**（`network_mode: host`，`:5218` / `:5198`）。**Actio / LLM / Embedding** 指向 **236**。

**Cloud Agent 访问 207**：见仓库根 `AGENTS.md` — Tailscale 入网后 SSH `wuzihan@100.101.46.52`（`qx-ai-07`，与 `10.168.1.207` 同机）；subnet `10.168.1.0/24` 需在 Tailscale Admin 批准后才可 ping `10.168.1.207`。

## 前置

- 主机可访问 ACR、`36.103.198.236:8600`（Actio）及 236 上的模型 HTTP 端点（见 `host.env` 里从 236 复制的 `deploy.env` 片段）。
- Docker + Docker Compose v2。
- 平台源码：本仓库根目录，或 Gitea `castmeta-research/viknow-platform`。
- viknow-storage 源码：与 236 同布局，默认 `../viknow-storage`（相对本仓库）或通过 `VIKNOW_STORAGE_ROOT` 指定。

## 快速执行（在 207 上，仓库根目录）

**推荐：236 打镜像 → ACR → 207 只 pull**（不要经 Tailscale 传源码在 207 build）。

在 **236**：

```bash
export ACR_PASSWORD=…   # 勿提交
bash deploy/207/push-images-236.sh
```

在 **207** `~/viknow207.host.env` 写入脚本输出的：

- `VIKNOW_PLATFORM_IMAGE=registry.cn-hangzhou.aliyuncs.com/litesense/viknow-platform-playground:207-latest`
- `VIKNOW_STORAGE_IMAGE=registry.cn-hangzhou.aliyuncs.com/litesense/viknow-storage:207-latest`

然后：

```bash
export VIKNOW207_HOST_ENV=~/viknow207.host.env
bash deploy/207/deploy-207.sh
```

（infra 仍用 ACR 的 PG/Redis/Neo4j/MinIO；**不再**部署 JuiceFS gateway / 独立 test MinIO。）

## 快速执行（旧：207 本地 build，慢，不推荐）

```bash
cp deploy/207/host.env.example ~/viknow207.host.env
# 编辑 ~/viknow207.host.env：POSTGRES_PASSWORD、REDIS_PASSWORD、NEO4J 密码、VIKNOW_APP_KEY、STORAGE_TOKEN、从 236 复制的模型 Key 等

export VIKNOW207_HOST_ENV=~/viknow207.host.env
bash deploy/207/deploy-207.sh
```

成功标志：

- `curl -sf http://127.0.0.1:5219/health`
- `curl -sf http://127.0.0.1:5218/docs`
- `curl -sf http://127.0.0.1:5198/playground`

## 端口冲突

`deploy-207.sh` 会停止占用 **16432 / 16479 / 17487 / 5198 / 5218 / 5219** 的容器（按名称前缀 `viknow207-` 与常见旧名）。若仍有冲突，手动 `docker ps` 后下线旧服务。

## 从 236 搬运镜像（207 无法直连 ACR 时）

在 236 上已 `docker pull` 后：

```bash
docker save \
  registry.cn-hangzhou.aliyuncs.com/litesense/viknow2-pgvector-textsearch:pg18-trixie-v1.3.1 \
  registry.cn-hangzhou.aliyuncs.com/litesense/viknow2-redis:7 \
  registry.cn-hangzhou.aliyuncs.com/litesense/viknow2-neo4j:5.26.3.0 \
  registry.cn-hangzhou.aliyuncs.com/litesense/viknow2-minio:latest \
  | ssh wuzihan@10.168.1.207 'docker load'
```

## 文件说明

| 文件 | 作用 |
|------|------|
| `host.env.example` | 207 宿主机密钥与路径（不提交真实 `host.env`） |
| `docker-compose.stack.yml` | 全栈 compose（infra + storage + platform） |
| `docker-compose.infra.yml` | 仅 PG / Redis / Neo4j（兼容旧文档） |
| `docker.sh` | 自动 `sudo docker`（207 默认无 docker 组） |
| `acr-pull.sh` | ACR 登录与拉取（密码仅通过环境变量 `ACR_PASSWORD`） |
| `init-databases.sh` | 创建 `viknow_access` / `viknow_agent` / `viknow_knowledge` / `viknow_storage` |
| `env/*` | Playground 进程 env（207 IP / 236 远程 Actio） |
| `deploy-207.sh` | 主编排 |
