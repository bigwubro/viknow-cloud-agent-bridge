# ViKnow jinhe 客户部署包

目录：`P60_SHARE/auto_download/app/viknow2-for-jinhe-911/`

本包用于 **客户现场已有 Docker 容器网络** 部署 ViKnow jinhe 版（无 Langfuse、无 Token Gateway，直连 LiteLLM）。

## 包内文件

| 文件 | 说明 |
|------|------|
| `docker-compose.yml` | 服务定义；挂 external data 网络 |
| `deploy.env` | 运行时配置与密钥（**部署前必须编辑**） |
| `README.md` | 本说明 |

镜像 tar 请放在同目录或 `viknow-deploy-image/` 子目录（另行上传）。

---

## 部署前准备

### 1. 确认 data 栈已运行

客户侧 Postgres / Redis / Minio / Neo4j 等 **已由 docker compose 启动**。

查 data 栈 Docker 网络名：

```bash
docker network ls
docker inspect <redis容器名> --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}'
```

记下网络名，例如 `customer-data_default`。

### 2. 编辑 deploy.env

**必改项：**

- `POSTGRES_HOST` / `REDIS_HOST` / `MINIO_ENDPOINT` / `NEO4J_URI`  
  → 改成客户 compose 里的 **services 名**（默认 `postgres` `redis` `minio` `neo4j`）
- `POSTGRES_PORT=5432`、`REDIS_PORT=6379` 等 → 用 **容器内端口**，不是宿主机映射端口
- `POSTGRES_PASSWORD`、`REDIS_PASSWORD`、`MINIO_ACCESS_KEY`、`MINIO_SECRET_KEY`、`NEO4J_PASSWORD`
- `VIKNOW_PRIVATE_BASE_URL` → 客户 LiteLLM OpenAI-compatible 地址（`/v1` 结尾）
- `VIKNOW_PRIVATE_API_KEY` → LiteLLM API Key
- `VIKNOW_ADMIN_PASSWORD` → 管理后台密码

**若 embedding/rerank 也在容器网内**，改：

- `VIKNOW_EMBEDDING_BASE_URL=http://<服务名>:<端口>/v1`
- `HYPERRAG_RERANK_BASE_URL=http://<服务名>:<端口>/v1`

### 3. 注意事项（重要）

1. **deploy.env 注释规则**  
   只能 `#` 单独成行；**禁止** `KEY=value # 注释`，否则 docker 会把 `#` 后内容读进变量导致启动失败。

2. **必须设置网络名**  
   compose 依赖环境变量 `VIKNOW_DATA_NETWORK`，未设置会直接报错。

3. **privileged: true**  
   compose 已开启，用于容器内 JuiceFS mount（与 236 viknow2-test 一致）。若客户禁用 privileged，需另行评估 JuiceFS 方案。

4. **Redis db0**  
   `REDIS_DATABASE=0` 用于统一调度 leader lease；多实例共用 Redis 时需协调 db 编号。

5. **jinhe 开关**  
   保持 `VIKNOW_LANGFUSE_ENABLED=false`、`HYPERRAG_PROMPT_ENV=jinhe`；不要配置 Token Gateway 相关变量。

6. **密钥勿提交 Git**  
   `deploy.env` 填好后仅保存在客户服务器，不要回传到代码仓。

---

## 部署命令

在客户服务器上，将本目录放到例如 `/opt/viknow2-jinhe/`，然后：

```bash
cd /opt/viknow2-jinhe

# 1. 加载镜像（若尚未 load）
docker load -i viknow-deploy-image/viknow2-app-jinhe-*.tar

# 2. 设置 compose 变量（必设网络名）
export VIKNOW_DATA_NETWORK=customer-data_default
export VIKNOW_IMAGE=viknow2-app:jinhe-51742-b497cc365b67
export VIKNOW_HOST_PORT=8000

# 3. 确认 deploy.env 已编辑
grep -E '^(POSTGRES_PASSWORD|VIKNOW_PRIVATE_BASE_URL|VIKNOW_PRIVATE_API_KEY)=' deploy.env

# 4. 启动
docker compose -f docker-compose.yml up -d

# 5. 查看状态
docker compose -f docker-compose.yml ps
docker logs -f viknow2-jinhe
```

### 验证

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/docs
```

返回 `200` 表示 API 已起来。

### 停止 / 重启

```bash
docker compose -f docker-compose.yml down
docker compose -f docker-compose.yml up -d
```

**注意：** `docker compose down` 默认 **不删** 命名卷 `viknow_jinhe_state`；要清空 state 需加 `-v`（会丢应用内部状态）。

---

## 目录结构建议（客户服务器）

```
/opt/viknow2-jinhe/
├── docker-compose.yml
├── deploy.env              # 含密钥，权限建议 chmod 600
├── data/                   # 自动创建；业务数据
└── viknow-deploy-image/
    └── viknow2-app-*.tar
```

首次启动前可创建数据目录：

```bash
mkdir -p data
chmod 755 data
chmod 600 deploy.env
```

---

## 与 236 联调差异

| 项 | 236 5177 联调 | 本客户包 |
|----|---------------|----------|
| 连中间件 | `host.docker.internal` + 映射端口 | compose 服务名 + 容器内端口 |
| 网络 | 单容器 bridge + extra_hosts | external data 网络 |
| 配置 | 旧镜像 bind-mount jinhe | 镜像内 bake jinhe |
| Langfuse / Gateway | 联调环境可能开启 | **关闭** |

---

## 常见问题

**Q: `set VIKNOW_DATA_NETWORK to your data stack network`**  
A: 未 export 网络名，或网络不存在。先 `docker network ls` 确认。

**Q: 连不上 Redis/Postgres**  
A: 检查 ViKnow 容器是否在 data 网络：`docker inspect viknow2-jinhe --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}'`

**Q: pydantic 报 enum 值非法**  
A: 多半是 deploy.env 行尾写了 `# 注释`，删掉行尾注释。

**Q: JuiceFS mount failed**  
A: 需 `privileged: true` 且 `/data` 可写；检查 Minio/PG 地址密码是否正确。

---

更新日期：2026-09-11
