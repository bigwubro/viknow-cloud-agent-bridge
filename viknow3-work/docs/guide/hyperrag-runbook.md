# HyperRAG 运行（viknow3）

## 配置原则

- **运维只改 `viknow3_ops` 数据库**（schema `ops`，或 `/api/v1/om/config` / SDK `client.om`）。
- **仓库里不要维护 `hyperrag.yml` / `app.yaml`**。进程启动时从 ops 表物化到 `VIKNOW_STATE_DIR/hyperrag/hyperrag.yml`，仅供 HyperRAG 子模块 `load_config_from_yaml` 读取（实现细节，不是给人编辑的配置文件）。

### Postgres / Redis / Neo4j 各是什么

| 组件 | 要不要「单独再建一个库」 | 说明 |
| --- | --- | --- |
| **ViKnow 3 业务 Postgres**（`16432` 上的 `viknow3_access` / `viknow3_agent` / `viknow3_knowledge`） | 已有新实例即可 | 由 `VIKNOW_*_DSN` 直连，**不在** ops YAML 里配 |
| **`viknow3_ops` Postgres**（通常同实例上的 `viknow3_ops` 库，schema `ops`） | 一个库就够 | 存 OM 表；含 `postgres_connection` / `redis_connection` / `neo4j_connection` 等**连接描述** |
| **`postgres_connection`（ops 表）** | 同 PG 实例上的**另一个 database** | ViKnow 3 开发默认 **`16432/viknow3_knowledge`**（与 viknow2-test 的 `viknow2` 隔离） |
| **Redis** | 需要**独立 Redis 进程** | ViKnow 3 开发：`deploy/viknow3-dev-data` → **`127.0.0.1:16479`** |
| **Neo4j** | 需要**独立 Neo4j 进程** | 同上 compose → **`neo4j://127.0.0.1:17487`** |

236 上先起专用 Redis/Neo4j：`docker compose -f deploy/viknow3-dev-data/docker-compose.yml up -d`，详见 `deploy/viknow3-dev-data/README.md`。

## 前置

- Python **3.12+**（与 `external/HyperRAG` 一致）
- ViKnow 3 **专用** PG（`:16432`）+ Redis（`:16479`）+ Neo4j（`:17487`）；模型/vLLM 端点仍可按环境指向 test 网关
- **`VIKNOW_OPS_DSN`** 指向 `viknow3_ops`，**`VIKNOW_OPS_SECRET`** 为 Fernet 密钥（解密库内 api_key 等）

## 环境

```bash
# 236 test 栈示例（host 用 127.0.0.1）
export VIKNOW_OPS_DSN='postgresql://.../viknow3_ops'
export VIKNOW_OPS_SECRET='...'   # 与 ops 库加密一致
export VIKNOW3_DATA_DIR=/data/viknow
export VIKNOW_STATE_DIR=/tmp/viknow3-state
export VIKNOW_HYPERRAG_REPO_ROOT="$PWD/external/HyperRAG"
export PYTHONPATH=src
```

在 `viknow3_ops` 的 schema `ops` 中填好（至少）：`postgres_connection`、`redis_connection`、`neo4j_connection`、`model_provider`（private / embedding / rerank）、`retrieval_setting`。

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
- `VIKNOW3_DATA_DIR=/data/viknow`（`working_dir` 指向 `knowledge/libraries`）

**不要**再挂载仓库内的 `config/hyperrag.yml`。
