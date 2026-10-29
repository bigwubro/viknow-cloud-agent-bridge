# Playground 236 — 环境变量规范（方案 2）

Playground **产品** = 浏览器静态页 + 调用 ViKnow API；**不负责** HyperRAG / 索引 / Agent / 存储实现。  
环境文件按 **进程** 拆成 `ui.env`、`api.env`、`secrets.env`（见下表）。

## 文件与进程

| 文件 | 提交到 Git | 注入方式 | 谁读 |
|------|------------|----------|------|
| **`ui.env`** | 是（无密钥） | `docker run --env-file` + UI 子进程仅透传本文件键 | `python -m ui.server` |
| **`api.env`** | 是（无密钥） | `docker run --env-file` | `python -m viknow3`（及 entrypoint 内 MinerU 等） |
| **`secrets.env`** | **否**（仅私有仓/236 宿主机） | `--env-file`（**后加载覆盖** `api.env` 同名键） | 主要是 API；`VIKNOW3_APP_KEY` 同时给 UI `runtime-config.js` |

`docker run` 顺序固定为：

```text
--env-file api.env --env-file secrets.env --env-file ui.env
```

同名键：**后文件 wins**（`ui.env` 只含 UI 键，不与 API 争用）。

## `ui.env` — 仅 Playground UI 进程

浏览器侧逻辑：

- 静态资源与 `runtime-config.js`（`VIKNOW_API_BASE` = `VIKNOW_PLAYGROUND_API_URL`）
- 所有 `/api/v1/*` 请求发往 ViKnow API
- 对象存储：**不**从 UI env 读 gateway 地址；上传走 API `…/uploads/direct`，下载/预览用 API `…/downloads` 返回的 **预签名 URL** 再直连存储

| 变量 | 含义 |
|------|------|
| `VIKNOW_UI_HOST` | UI 监听地址 |
| `VIKNOW_UI_PORT` | UI 端口（236 默认 `5198`） |
| `VIKNOW_PLAYGROUND_API_URL` | 浏览器访问的 ViKnow API 根 URL（须公网可达，如 `http://36.103.198.236:5218`） |

可选（**仅** `secrets.env`，不写进 `ui.env`）：

| 变量 | 含义 |
|------|------|
| `VIKNOW3_APP_KEY` | 注入 `runtime-config.js` 的默认 `X-App-Key`（entrypoint 从容器 env 传给 UI 子进程） |

**禁止**出现在 UI 子进程 env 的业务键（示例）：  
`VIKNOW_STORAGE_*`、`VIKNOW_S3_*`、`ACTIO_*`、`VIKNOW_HYPERRAG_*`、`VIKNOW3_DATA_DIR`、`VIKNOW_SCRATCH_DIR`、`VIKNOW_CORS_*`（CORS 在 API）、MinerU / HyperRAG 镜像等。

Entrypoint 为 UI 额外设置的 **镜像引导**（不属于业务 env 文件）：`PYTHONPATH`（仅为 `python -m ui.server` 能 import 包）。

## `api.env` — ViKnow 接入层 + 演示栈（5218）

| 类别 | 变量（236 默认见 `api.env.example`） |
|------|--------------------------------------|
| 监听 | `VIKNOW_HOST`, `VIKNOW_PORT` |
| 跨域 | `VIKNOW_CORS_ORIGINS`（允许 Playground UI Origin） |
| 数据路径 | `VIKNOW3_DATA_DIR`, `VIKNOW3_LIBRARIES_ROOT`, `VIKNOW_SCRATCH_DIR`, `VIKNOW_AGENT_WORKSPACE_ROOT`, `VIKNOW_STATE_DIR`, `VIKNOW_VENDOR_FILES_ROOT` |
| 存储（**API** 代传 / 预签名） | `VIKNOW_STORAGE_URL`, `VIKNOW_STORAGE_PUBLIC_*`, `VIKNOW_STORAGE_BROWSER_URL`, `VIKNOW_S3_PUBLIC_ENDPOINT` |
| HyperRAG / MinerU / Actio | `VIKNOW_HYPERRAG_REPO_ROOT`, `VIKNOW_MINERU_*`, `VIKNOW_E2E_MINERU_WRAPPER`, `HYPERRAG_TEST_IMAGE`, `ACTIO_SERVER_URL` |
| 容器内路径 | `VIKNOW3_ROOT`, `PYTHONPATH`（API 进程） |

模型 Key、DSN、storage token 等 **只** 在 `secrets.env`。

## `secrets.env`

见 `secrets.env.example`。由 `refresh-secrets-from-host.sh` 从 236 宿主机合并生成时：

- **删除** 已迁到 `ui.env` / `api.env` 的键（端口、公网 URL、Actio 强制 `:8600` 等脚本已处理）
- **勿** 把 `VIKNOW_PLAYGROUND_API_URL` 写入 secrets（归属 `ui.env`）

## 宿主机回滚（非容器）

`scripts/deploy-playground-ui.sh`：

- `start-api`：`deploy/236/env/api.env` + `secrets.env` +（可选）`VIKNOW3_ENV_FILE` 链
- `start-ui`：**仅** `deploy/236/env/ui.env` + 从 `secrets.env` 读取的 `VIKNOW3_APP_KEY`
