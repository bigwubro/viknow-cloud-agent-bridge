# 外部基础设施（不由 viknow3 仓库启动）

**Postgres / Redis / Neo4j / 对象网关** 由独立运维任务或环境 provision 好后，viknow3 **只通过 env 与 ops 表连接**，本仓 **不包含** docker-compose 起库脚本。

## Playground / 接入层进程需要的环境

| 类型 | 变量 / 来源 | 说明 |
|------|-------------|------|
| 业务 Postgres | `VIKNOW_ACCESS_DSN`、`VIKNOW_AGENT_DSN`、`VIKNOW_HYPERRAG_DSN` | 见 `deploy/236/env/secrets.env`（refresh 从宿主机合并） |
| 运维 Postgres | `VIKNOW_OPS_DSN` | `viknow_ops` 内 `s3_client`、`postgres_connection`、`redis_connection`、`neo4j_connection` 等 |
| viknow-storage | `VIKNOW_STORAGE_URL` + `VIKNOW_STORAGE_TOKEN` | `api.env` + secrets |
| S3 预签名公网 | `VIKNOW_S3_PUBLIC_ENDPOINT` | `api.env` |

HyperRAG 运行时从 **ops 表** 解析 Redis/Neo4j/embedding，**不要**在 viknow3 仓里再起 Redis compose。

## 236 Playground 容器

- 部署：**仅** `deploy/236/`（`docker build` + `deploy.sh` + `ui.env` / `api.env` / `secrets.env`）。
- 假定 236 宿主机上 **已有** 可达的 PG/Redis/Neo4j（由其它任务维护）；容器 `--network host` 用 DSN 里的 `127.0.0.1:端口` 连接。

## 单测

设置 `VIKNOW_TEST_PG_PASSWORD` 或 `VIKNOW_TEST_PG_BASE`，或在本机 `~/.viknow3/ops.env` / `deploy/demo/pg-demo.env` 中提供 DSN 前缀；测试库名后缀 `*_test`（见 `tests/pg_test_env.py`）。**单测不会启动数据库。**

## 手工 SQL（可选）

历史迁移脚本仅供参考 / 一次性运维，**不是** viknow3 启动步骤：

- `docs/infra/migrations/phase1-merge-catalog-into-knowledge.sql`
- `docs/infra/migrations/knowledge-index-jobs.sql`

应用启动时多数表由 `store._ensure()` 等代码路径维护；见 `docs/knowledge-index-persistence.md`。
