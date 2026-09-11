# ViKnow jinhe 客户现场交付包

与 `config/jinhe/` 配套，用于 **接入已有 Docker 容器网络** 的外部部署（不是 236 的 `host.docker.internal` 联调模式）。

## 文件

| 文件 | 说明 |
|------|------|
| `docker-compose.yml` | 挂 external network，无 `extra_hosts` |
| `deploy.env` | 运行时密钥与中间件地址（见 jinhe 模板） |

## 启动前

1. `export VIKNOW_DATA_NETWORK=<现场 data 栈网络名>`
2. `export VIKNOW_IMAGE=<镜像 tag>`
3. 编辑 `deploy.env`：中间件用 **compose 服务名 + 容器内端口**

```bash
REDIS_HOST=redis
REDIS_PORT=6379
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
MINIO_ENDPOINT=minio:9000
NEO4J_URI=neo4j://neo4j:7687
```

4. `docker compose -f docker-compose.yml up -d`

## 236 联调

5177 仍用 `/home/cursor-agent/docker-compose.viknow2-test.manual.yml`（`extra_hosts` + 映射端口）。
