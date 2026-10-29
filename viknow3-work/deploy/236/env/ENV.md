# Playground 236 — 环境变量规范（方案 2）

Playground **产品** = 浏览器静态页 + 调用 ViKnow API；**不负责** HyperRAG / 索引 / Agent / 存储实现。  
环境文件按 **进程** 拆成 `ui.env`、`api.env`、`secrets.env`（见下表）。

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

`VIKNOW3_APP_KEY` 只在 **`secrets.env`**，由 entrypoint 传给 UI 生成 `runtime-config.js`。

## `api.env`（236 Playground 容器）

| 变量 | 用途 |
|------|------|
| `VIKNOW_HOST` / `VIKNOW_PORT` | API 监听（5218） |
| `VIKNOW_CORS_ORIGINS` | API 允许 Playground UI Origin |
| `VIKNOW_STORAGE_URL` | viknow-storage 网关（内网，与 `VIKNOW_STORAGE_TOKEN` 成对） |
| `VIKNOW_S3_PUBLIC_ENDPOINT` | 预签名 URL 的浏览器可达 host（`:19191`） |
| `VIKNOW3_DATA_DIR` | MinerU wrapper 等数据根 |
| `VIKNOW3_LIBRARIES_ROOT` | 知识库目录 |
| `VIKNOW_SCRATCH_DIR` | HyperRAG scratch / object gateway |
| `VIKNOW_AGENT_WORKSPACE_ROOT` | Agent session workspace |
| `VIKNOW_STATE_DIR` | HyperRAG 状态目录（挂载 `/state`） |
| `VIKNOW_VENDOR_FILES_ROOT` | DeepAgents vendor 文件 |
| `HYPERRAG_TEST_IMAGE` | `scripts/mineru-docker-wrapper` 镜像 |
| `ACTIO_SERVER_URL` | Actio 服务（236 固定 `127.0.0.1:8600`） |

**已移除、代码不读的键（勿再加回）**：`VIKNOW_STORAGE_PUBLIC_*`、`VIKNOW_STORAGE_BROWSER_URL`、`VIKNOW_E2E_MINERU_WRAPPER`（由部署脚本运行时设置）、与 Dockerfile 重复的 `PYTHONPATH` / `VIKNOW3_ROOT` / `VIKNOW_HYPERRAG_REPO_ROOT` / `VIKNOW_MINERU_BIN_DIR`。

对象存储凭证与 DSN、模型 Key 等 **只** 在 `secrets.env`（`refresh-secrets-from-host.sh` 会从 secrets 里删掉上述非密钥键）。

## `secrets.env`

见 `secrets.env.example`。典型必填：

- `VIKNOW_ACCESS_DSN` / `VIKNOW_AGENT_DSN` / `VIKNOW_HYPERRAG_DSN`
- `VIKNOW_OPS_DSN`（ops / S3 客户端配置）
- `VIKNOW_STORAGE_TOKEN`（与 `VIKNOW_STORAGE_URL` 配对）
- `VIKNOW3_APP_KEY`
- 以及 `deploy.env` 中的模型 / Actio API key 等（由 refresh 脚本合并）

## 宿主机回滚（非容器）

`scripts/deploy-playground-ui.sh`：`start-api` 读 `api.env` + `secrets.env`；`start-ui` 只读 `ui.env` + App Key。
