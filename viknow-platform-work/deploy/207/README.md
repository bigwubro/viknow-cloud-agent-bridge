# ViKnow Platform + Storage on 10.168.1.207

207 单机栈：**Postgres / Redis / Neo4j**（ACR 镜像）+ **viknow-storage**（内置 MinIO，不对 platform 暴露 JuiceFS/独立 MinIO）+ **Playground**（API `:5218`、UI `:5198`）。**Actio 与 LLM/Embedding** 指向 **236**（`36.103.198.236`），不在 207 部署。

## 前置

- 主机可访问 ACR、`36.103.198.236:8600`（Actio）及 236 上的模型 HTTP 端点（见 `host.env` 里从 236 复制的 `deploy.env` 片段）。
- Docker + Docker Compose v2。
- 平台源码：本仓库根目录，或 Gitea `castmeta-research/viknow-platform`。
- viknow-storage 源码：与 236 同布局，默认 `../viknow-storage`（相对本仓库）或通过 `VIKNOW_STORAGE_ROOT` 指定。

## 快速执行（在 207 上，仓库根目录）

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
| `docker-compose.infra.yml` | PG / Redis / Neo4j |
| `acr-pull.sh` | ACR 登录与拉取（密码仅通过环境变量 `ACR_PASSWORD`） |
| `init-databases.sh` | 创建 `viknow_access` / `viknow_agent` / `viknow_knowledge` / `viknow_storage` |
| `env/*` | Playground 进程 env（207 IP / 236 远程 Actio） |
| `deploy-207.sh` | 主编排 |
