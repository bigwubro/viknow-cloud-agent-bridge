# Playground 236 — 环境变量规范（方案 2）

Playground **产品** = 浏览器静态页 + 调用 ViKnow API；**不负责** HyperRAG / 索引 / Agent / 存储实现。  
环境文件按 **进程** 拆成 `ui.env`、`api.env`、`secrets.env`（见下表）。

## 没有 `playground.env`

历史上曾用 **`deploy/236/env/playground.env`** 把 UI + API + 密钥写进一个文件，已 **删除**。  
若文档或脚本仍提到 `playground.env`，一律改为 **`ui.env` + `api.env` + `secrets.env`**（口语里的「Playground 部署 env」指这三份，不是第四个文件名）。

## 文件与进程

| 文件 | 提交到 Git | 注入方式 | 谁读 |
|------|------------|----------|------|
| **`ui.env`** | 是（无密钥） | `docker run --env-file` + UI 子进程仅透传本文件键 | `python -m ui.server` |
| **`api.env`** | 是（无密钥） | `docker run --env-file` | `python -m viknow3`（及 entrypoint 内 MinerU 等） |
| **`secrets.env`** | **否**（仅私有仓/236 宿主机） | `--env-file`（**后加载覆盖** `api.env` 同名键） | 主要是 API；`VIKNOW3_APP_KEY` 同时给 UI `runtime-config.js` |

`docker run` 顺序：

```text
--env-file api.env --env-file secrets.env --env-file ui.env
```

镜像内 **`PYTHONPATH` / `VIKNOW3_ROOT` / `VIKNOW_HYPERRAG_REPO_ROOT` / `PATH`** 由 `deploy/236/Dockerfile` 设置，**不要**写进 `api.env`。

## `ui.env`

| 变量 | 含义 |
|------|------|
| `VIKNOW_UI_HOST` | UI 监听地址 |
| `VIKNOW_UI_PORT` | UI 端口（236 默认 `5198`） |
| `VIKNOW_PLAYGROUND_API_URL` | 浏览器访问的 ViKnow API 根 URL |
| `VIKNOW_STORAGE_URL` | 浏览器直连 **viknow-storage** 的根 URL（需 CORS 允许 Playground Origin） |

`VIKNOW3_APP_KEY` 与 **`VIKNOW_STORAGE_TOKEN`** 在 **`secrets.env`**，由 UI 进程注入 `runtime-config.js`（Playground JS 调 API 用 `X-App-Key`，调 storage 用 `Authorization: Bearer`）。**当前 demo 允许 token 进浏览器；后续可改为短 token。**

## `api.env`（236 Playground 容器）

| 变量 | 用途 |
|------|------|
| `VIKNOW_HOST` / `VIKNOW_PORT` | API 监听（5218） |
| `VIKNOW_CORS_ORIGINS` | API 允许 Playground UI Origin |
| `VIKNOW_STORAGE_URL` | viknow-storage（**API 服务端**，常 `127.0.0.1:5219`；浏览器用 `ui.env` 里的对外 URL） |
| `VIKNOW3_DATA_DIR` | 宿主机数据根（MinerU wrapper 等 bind-mount 上层） |
| `VIKNOW3_LIBRARIES_ROOT` | HyperRAG **磁盘 working_dir**（不是 catalog；对象在 viknow-storage） |
| `VIKNOW_SCRATCH_DIR` | 索引 scratch、jieba/TMP |
| `VIKNOW_AGENT_WORKSPACE_ROOT` | Agent 每 session 工作区根（`sessions/{id}/workspace/`） |
| `VIKNOW_STATE_DIR` | HyperRAG 状态目录（**容器**内为挂载 `/state`；**宿主机** `deploy-playground-ui.sh` 在 `/state` 不可写时自动改为 `/home/cursor-agent/viknow3/state`） |
| `VIKNOW_VENDOR_FILES_ROOT` | DeepAgents vendor（容器 `/state/vendor-files`；宿主机随 `VIKNOW_STATE_DIR` 映射） |
| `HYPERRAG_TEST_IMAGE` | `scripts/mineru-docker-wrapper` 镜像 |
| `ACTIO_SERVER_URL` | Actio 服务（236 固定 `127.0.0.1:8600`） |

**对象存储**：Playground **浏览器**上传/预览/下载 **直连 viknow-storage**（`ui.env` + `runtime-config.js`）。viknow3 **不提供** `uploads/direct` 或 `…/preview|content` 代理路由；catalog 用 `POST …/assets` + 服务端 `asset_exists` 校验。API 索引时仍用 server token 从 storage 拉 scratch。

**禁止**：viknow3 进程 **不得** 直连 MinIO/S3；**不要** 配置 `VIKNOW_S3_PUBLIC_ENDPOINT` 或 `VIKNOW_STORAGE_PUBLIC_*`。

**已移除、代码不读的键（勿再加回）**：`VIKNOW_S3_PUBLIC_ENDPOINT`、`VIKNOW_STORAGE_PUBLIC_*`、`VIKNOW_STORAGE_BROWSER_URL`、`VIKNOW_E2E_MINERU_WRAPPER`（由部署脚本运行时设置）、与 Dockerfile 重复的 `PYTHONPATH` / `VIKNOW3_ROOT` / `VIKNOW_HYPERRAG_REPO_ROOT` / `VIKNOW_MINERU_BIN_DIR`。

对象存储凭证与 DSN、模型 Key 等 **只** 在 `secrets.env`（`refresh-secrets-from-host.sh` 会从 secrets 里删掉上述非密钥键）。

**Playground 236 不使用飞书**：`VIKNOW_FEISHU_*` / `FEISHU_*` 从宿主机 merge 后由 refresh **删除**（见 `docs/project-boundary.md`）。

**236 宿主机磁盘布局（`api.env`，与 Playground UI / Postgres `library_id` 无关）**：

```text
/data/viknow/                          # VIKNOW3_DATA_DIR
/data/viknow/hyperrag-wd/              # VIKNOW3_LIBRARIES_ROOT（HyperRAG working_dir）
/data/viknow/scratch/                  # VIKNOW_SCRATCH_DIR
/data/viknow/agent-workspaces/         # VIKNOW_AGENT_WORKSPACE_ROOT
  sessions/{session_id}/workspace/…
```

旧路径 `…/knowledge/libraries/playground-cursor-agent/…` 与 `…/playground/…` 已废弃。

## `secrets.env`

见 `secrets.env.example`。典型必填：

- `VIKNOW_ACCESS_DSN` / `VIKNOW_AGENT_DSN` / `VIKNOW_HYPERRAG_DSN`
- `VIKNOW_OPS_DSN`（ops / S3 客户端配置）
- `VIKNOW_STORAGE_TOKEN`（与 `VIKNOW_STORAGE_URL` 配对）
- `VIKNOW3_APP_KEY`
- 以及 `deploy.env` 中的模型 / Actio API key 等（由 refresh 脚本合并）

## 宿主机回滚（非容器）

`scripts/deploy-playground-ui.sh`：`start-api` 读 `api.env` + `secrets.env`；`start-ui` 只读 `ui.env` + App Key。

---

## 配置审计（哪些有用、哪些会被 refresh 删掉）

### `ui.env` — 仅 3 键（全部有用）

| 变量 | 读者 |
|------|------|
| `VIKNOW_UI_HOST` / `VIKNOW_UI_PORT` | `ui.server` |
| `VIKNOW_PLAYGROUND_API_URL` | `runtime-config.js` → 浏览器调 API |

### `api.env` — Playground 236 保留键（全部有用）

| 变量 | 读者 |
|------|------|
| `VIKNOW_HOST` / `VIKNOW_PORT` | `python -m viknow3` |
| `VIKNOW_CORS_ORIGINS` | API CORS（Playground UI Origin） |
| `VIKNOW_STORAGE_URL` | viknow-storage（服务端）；浏览器不经 MinIO |
| `VIKNOW3_DATA_DIR` / `VIKNOW3_LIBRARIES_ROOT` / `VIKNOW_SCRATCH_DIR` / `VIKNOW_AGENT_WORKSPACE_ROOT` | HyperRAG / Agent 路径 |
| `VIKNOW_STATE_DIR` / `VIKNOW_VENDOR_FILES_ROOT` | HyperRAG 状态、DeepAgents vendor |
| `HYPERRAG_TEST_IMAGE` | `scripts/mineru-docker-wrapper` |
| `ACTIO_SERVER_URL` | Actio 客户端（entrypoint 会规范化 host/port） |

**不要写进 `api.env`**：`VIKNOW_PLAYGROUND_CORS_ORIGINS`（与 `VIKNOW_CORS_ORIGINS` 重复，代码里仅作别名 fallback）。

### `secrets.env` — 应保留（refresh 之后典型）

| 类别 | 变量 |
|------|------|
| 业务库 | `VIKNOW_ACCESS_DSN` / `VIKNOW_AGENT_DSN` / `VIKNOW_HYPERRAG_DSN` |
| 运维库 | `VIKNOW_OPS_DSN` / `VIKNOW_OPS_SECRET` |
| 存储 | `VIKNOW_STORAGE_TOKEN` |
| Playground | `VIKNOW3_APP_KEY` |
| 模型 / 嵌入 | `VIKNOW_PRIVATE_*` / `VIKNOW_EMBEDDING_*` / `VIKNOW3_CHAT_*` / `VIKNOW3_EMBED_*` 等（来自宿主机 `deploy.env`） |
| Actio | `ACTIO_API_KEY` |
| Langfuse | `VIKNOW_LANGFUSE_*` / `VIKNOW_DEEPAGENTS_LANGFUSE_*` |
| HyperRAG 子进程 | `HYPERRAG_LOG_LEVEL` / `HYPERRAG_JOB_LOG_DIR` / `HYPERRAG_RERANK_*` 等 |

连接 **Postgres / Redis / Neo4j 实例** 的 host/port/password **应只在 `viknow_ops` 表**，不要指望 `secrets.env` 里的 `POSTGRES_*` / `REDIS_*` / `NEO4J_*`（refresh 会删）。

### `secrets.env` — refresh **自动删除**（deploy.env 遗留、viknow3 不读或已有替代）

| 删除原因 | 键 / 前缀 |
|----------|-----------|
| 属于 `api.env` / `ui.env` | 见 `refresh-secrets-from-host.sh` 第一段 `for key` |
| 飞书 | `VIKNOW_FEISHU_*` / `FEISHU_*` |
| 废弃 storage 浏览器 | `VIKNOW_STORAGE_PUBLIC_*` / `VIKNOW_STORAGE_BROWSER_URL` |
| 与 `VIKNOW3_DATA_DIR` 重复 | `VIKNOW_FILES_ROOT` |
| viknow2 / 监控 / 其它产品 | `VIKNOW1_*` / `VIKNOW_PROMETHEUS_*` / `VIKNOW_VLLM_MONITOR_*` / `JUICEFS_*` / `MINIO_*` |
| 应用层重复基础设施 | `POSTGRES_*` / `REDIS_*` / `NEO4J_*`（用 DSN + ops 表） |
| 单测 / 无效 | `VIKNOW_OPS_TEST_DSN` / `VIKNOW3_SDK_JS` / `VIKNOW_PERSISTENCE_BACKEND` / `VIKNOW_ADMIN_PASSWORD` / `VIKNOW_ASR_MODEL` / `VIKNOW_LOCATE_ANYTHING_BASE_URL` |

合并后可在 236 执行：

```bash
bash deploy/236/env/refresh-secrets-from-host.sh
grep -E '^[A-Za-z_][A-Za-z0-9_]*=' deploy/236/env/secrets.env | cut -d= -f1 | sort
```

核对键名是否只剩上表「应保留」范围。
