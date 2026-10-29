# HyperRAG 运行（viknow3）

## 配置原则

- **运维只改 `viknow_ops` 数据库**（或 `/api/v1/om/config` / SDK `client.om`）。
- **仓库里不要维护 `hyperrag.yml` / `app.yaml`**。进程启动时从 ops 表物化到 `VIKNOW_STATE_DIR/hyperrag/hyperrag.yml`，仅供 HyperRAG 子模块 `load_config_from_yaml` 读取（实现细节，不是给人编辑的配置文件）。

### Postgres / Redis / Neo4j 各是什么

| 组件 | 要不要「单独再建一个库」 | 说明 |
| --- | --- | --- |
| **ViKnow 3 业务 Postgres**（`16432` 上的 `viknow_access` / `viknow_agent` / `viknow_hyperrag`） | 已有新实例即可 | 由 `VIKNOW_*_DSN` 直连，**不在** ops YAML 里配 |
| **`viknow_ops` Postgres**（通常同实例上的 `viknow_ops` 库） | 一个库就够 | 存 OM 表；含 `postgres_connection` / `redis_connection` / `neo4j_connection` 等**连接描述** |
| **`postgres_connection`（ops 表）** | 同 PG 实例上的**另一个 database** | ViKnow 3 开发默认 **`16432/viknow3_knowledge`**（与 viknow2-test 的 `viknow2` 隔离） |
| **Redis** | 需要**独立 Redis 进程** | 连接信息在 ops 表 `redis_connection`；**由外部任务 provision**，见 [`docs/external-infra.md`](../external-infra.md) |
| **Neo4j** | 需要**独立 Neo4j 进程** | 连接信息在 ops 表 `neo4j_connection`；同上 |

236 上 Postgres/Redis/Neo4j **不由 viknow3 仓库启动**；Playground 容器启动前假定宿主机 DSN/ops 已指向可用实例。

## 前置

- Python **3.12+**（与 `external/HyperRAG` 一致）
- 外部已 provision 的 PG（`VIKNOW_*_DSN`）+ Redis/Neo4j（经 ops 表）；236 典型端口见 [`docs/external-infra.md`](../external-infra.md)
- **`VIKNOW_OPS_DSN`** 指向 `viknow_ops`，**`VIKNOW_OPS_SECRET`** 为 Fernet 密钥（解密库内 api_key 等）

## 环境

```bash
# 236 test 栈示例（host 用 127.0.0.1）
export VIKNOW_OPS_DSN='postgresql://.../viknow_ops'
export VIKNOW_OPS_SECRET='...'   # 与 ops 库加密一致
export VIKNOW3_DATA_DIR=/data/viknow
export VIKNOW_STATE_DIR=/tmp/viknow3-state
export VIKNOW_HYPERRAG_REPO_ROOT="$PWD/external/HyperRAG"
export PYTHONPATH=src
```

在 `viknow_ops` 中填好（至少）：`postgres_connection`、`redis_connection`、`neo4j_connection`、`model_provider`（private / embedding / rerank）、`retrieval_setting`。

安装引擎：

```bash
pip install -r requirements.txt -r requirements-hyperrag.txt
# 需要在本机跑 MinerU / 视频解析 / VAD 索引时再加：
# pip install -e "external/HyperRAG[index]"
```

## 启动 access（含 HyperRAG lifespan）

```bash
./scripts/run-hyperrag-smoke.sh
# 或: python -m viknow3
```

## 冒烟

```bash
curl -s -H "Authorization: Bearer dev-app-key" -H "Content-Type: application/json" \
  -d '{"library_id":"default","query":"测试"}' \
  http://127.0.0.1:5199/api/v1/knowledge/search
```

## Docker demo（viknow3-demo）

镜像需包含 `requirements-hyperrag.txt`，并设置：

- `VIKNOW_OPS_DSN` / `VIKNOW_OPS_SECRET`（以及 access / hyperrag 业务库 DSN 若与 demo 一致）
- `VIKNOW3_DATA_DIR=/data/viknow`，`VIKNOW3_LIBRARIES_ROOT=/data/viknow/hyperrag-wd`（HyperRAG `working_dir`）

**不要**再挂载仓库内的 `config/hyperrag.yml`。
