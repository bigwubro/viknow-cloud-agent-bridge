# 私有化 + 1Panel：部署 ViKnow Platform 全栈

面向 **Gitea `castmeta-research/viknow-platform`**（Playground 镜像）+ **`viknow-storage`**（对象网关），不是 viknow2-jinhe 客户镜像。

权威参考在代码仓内：

- `deploy/207/docker-compose.stack.yml` — 单机全栈形状（207 现场）
- `deploy/236/env/ENV.md` — `api.env` / `ui.env` / `secrets.env` 分工
- `docs/external-infra.md` — 本仓 **不** 起 PG/Redis/Neo4j 的边界说明

1Panel 上建议拆成 **多个「编排」**，共用一条 Docker 网络，按顺序启动。

---

## 1. 组件清单（要对都部署什么）

| 层级 | 组件 | 作用 | 1Panel 编排 |
|------|------|------|-------------|
| 数据 | **Postgres** | `viknow_ops`、`viknow_access`、`viknow_agent`、`viknow_knowledge`、`viknow_storage` 多库 | 栈 **01-infra** |
| 数据 | **Redis** | HyperRAG / 任务（连接信息在 **ops 表**，不是 platform env 里写 host） | 01 |
| 数据 | **Neo4j** | 图谱（同上，ops 表） | 01 |
| 存储 | **MinIO** | 对象字节（**不对浏览器暴露**） | 栈 **02-storage** |
| 存储 | **viknow-storage** | 库/资产 catalog API（Playground **浏览器直连**） | 02 |
| 应用 | **viknow-platform-playground** | API **5218** + UI **5198**（单容器双进程） | 栈 **03-platform** |
| 推理 | LiteLLM / embedding / rerank / Whisper / MinerU-VLM | 索引与对话（**secrets + ops 表**） | 栈 **04-models**（可选，或客户已有） |
| 媒体 | **Actio** | 视频切分等 | 栈 **05-actio**（可选） |

**不要**在 platform 容器里再配 `MINIO_*` / `POSTGRES_HOST` 这类 viknow2 风格变量；Platform 用 **DSN + `VIKNOW_OPS_DSN` + storage token**，Redis/Neo4j/S3 由 **ops 加密配置** 给 HyperRAG 用（`scripts/repair-ops-infra-secrets-from-host.sh`）。

---

## 2. 网络与命名（1Panel 必统一）

1. 在 **01-infra** 创建网络，固定名例如 **`viknow-platform_data`**（与 [`../stacks/01-data`](../stacks/01-data) 一致）。
2. **02 / 03 / 04 / 05** 的 compose 里 `networks.xxx.external: true`，`name: viknow-platform_data`。
3. 容器间用 **Compose service 名** 通信（`postgres:5432`、`storage:8080`）；**只有浏览器和 ui.env** 才写客户 IP/域名。

---

## 3. 1Panel 建栈顺序

```text
离线 docker load 全部镜像
    → 01-infra（Postgres 健康）
    → 初始化 PG 多库（一次性 SQL，见 §5）
    → 02-storage（storage 依赖 postgres + minio）
    → 04-models / 05-actio（若本机起推理与 Actio）
    → 写齐 03-platform 的 api / secrets / ui
    → ops 表灌入 Redis/Neo4j/S3（§6）
    → 03-platform up
    → 冒烟 §8
```

每个编排目录建议：

```text
/opt/viknow-platform-1panel/03-platform/
├── docker-compose.yml
├── .env                 # 仅 Compose：镜像 tag、卷路径、网络名
├── api.env              # 提交模板，无密钥
├── ui.env               # 浏览器可见 URL
└── secrets.env          # chmod 600，勿提交 Git
```

**1Panel 环境变量页** 一般对应 **`.env`（插值）**；`api.env` / `secrets.env` / `ui.env` 用 **文件挂载** 或 SFTP 编辑后 **强制重建** 容器。

`docker run` / compose 加载顺序（与 236 一致）：

```text
api.env → secrets.env → ui.env
```

---

## 4. 各栈要点

### 4.1 栈 01-infra

- 镜像可参考 platform 仓 `deploy/207/docker-compose.stack.yml` 的 `postgres` / `redis` / `neo4j`（或客户已有 PG，则 **跳过 01**，DSN 指向现网）。
- Postgres 建议单实例、用户 `viknow_ops`，默认库 `viknow_ops`。
- 宿主机映射端口仅用于运维；**platform 容器内 DSN 用 `postgres:5432`**。

模板：[`stacks-1panel/01-infra/docker-compose.yml`](stacks-1panel/01-infra/docker-compose.yml)

### 4.2 栈 02-storage

- 来源：**viknow-storage** 仓 `docker-compose.yml`（MinIO + storage）。
- 环境：
  - `STORAGE_DATABASE_URL=postgresql+psycopg://viknow_ops:PASSWORD@postgres:5432/viknow_storage`
  - `STORAGE_TOKEN` 与 platform `secrets.env` 里 **`VIKNOW_STORAGE_TOKEN` 相同**
  - `STORAGE_CORS_ORIGINS=http://<客户IP>:5198`（Playground UI Origin）
- 容器内监听 **8080**；映射宿主机 **5219:8080**（与 236 习惯一致，可改）。

**禁止** 给 platform 或浏览器配置 MinIO 公网地址；浏览器只认 **`VIKNOW_STORAGE_URL`**。

模板：[`stacks-1panel/02-storage/docker-compose.yml`](stacks-1panel/02-storage/docker-compose.yml)

### 4.3 栈 03-platform

- 镜像：`docker build -f deploy/236/Dockerfile` 产物（CI：**Gitea → Playground 236**）。
- **1Panel 不要用 `network_mode: host`**（207 栈为 236 特例）；用 bridge + 映射端口：
  - `5198:5198`（UI）
  - `5218:5218`（API）
- 仍需 **`privileged: true`**、`/var/run/docker.sock`（DeepAgents sandbox、可选 MinerU sidecar）。
- 挂载：
  - `/data/viknow` → HyperRAG working_dir、scratch、agent workspaces
  - `/state` → HyperRAG 状态、vendor-files

**api.env（容器内 / 服务端）示例：**

```bash
VIKNOW_HOST=0.0.0.0
VIKNOW_PORT=5218
VIKNOW_CORS_ORIGINS=http://192.168.1.10:5198
VIKNOW_STORAGE_URL=http://storage:8080
VIKNOW_DATA_DIR=/data/viknow
VIKNOW_LIBRARIES_ROOT=/data/viknow/hyperrag-wd
VIKNOW_SCRATCH_DIR=/data/viknow/scratch
VIKNOW_AGENT_WORKSPACE_ROOT=/data/viknow/agent-workspaces
VIKNOW_STATE_DIR=/state
VIKNOW_VENDOR_FILES_ROOT=/state/vendor-files
ACTIO_SERVER_URL=http://actio:8600
```

**ui.env（浏览器）示例：**

```bash
VIKNOW_UI_HOST=0.0.0.0
VIKNOW_UI_PORT=5198
VIKNOW_PLAYGROUND_API_URL=http://192.168.1.10:5218
VIKNOW_STORAGE_URL=http://192.168.1.10:5219
```

**secrets.env（必填键）：** 见 platform 仓 `deploy/236/env/secrets.env.example`  
DSN 在同一 Docker 网络内示例：

```bash
VIKNOW_ACCESS_DSN=postgresql://viknow_ops:PASSWORD@postgres:5432/viknow_access
VIKNOW_AGENT_DSN=postgresql://viknow_ops:PASSWORD@postgres:5432/viknow_agent
VIKNOW_HYPERRAG_DSN=postgresql://viknow_ops:PASSWORD@postgres:5432/viknow_knowledge
VIKNOW_OPS_DSN=postgresql://viknow_ops:PASSWORD@postgres:5432/viknow_ops
VIKNOW_OPS_SECRET=<随机 Fernet 密钥>
VIKNOW_STORAGE_TOKEN=<与 STORAGE_TOKEN 一致>
VIKNOW_APP_KEY=<Playground X-App-Key>
VIKNOW_PRIVATE_BASE_URL=http://litellm:4000/v1
VIKNOW_PRIVATE_API_KEY=...
ACTIO_API_KEY=...
```

模板：[`stacks-1panel/03-platform/docker-compose.yml`](stacks-1panel/03-platform/docker-compose.yml)

### 4.4 栈 04-models（推理）

Platform **不在 compose 里写死** embedding host；HyperRAG 从 **ops 表** 读 Redis/Neo4j/模型端点。你仍需在本机或可访问地址提供：

- 对话 / MinerU-VLM：`VIKNOW_PRIVATE_*`（secrets）
- Embedding / Rerank：写入 ops 或通过 OM 配置 API

做法二选一：

1. **独立 LiteLLM / vLLM compose**（与 viknow2 客户包类似），repair 脚本从 `deploy.env` 灌 ops；或  
2. 客户已有统一网关，只填 secrets + 调 `POST /api/v1/om/config`。

MinerU：**Playground 镜像已带 MinerU CLI**；VLM 走 HTTP（`MINERU_VL_*`，entrypoint 会从 private provider 推导）。Greenfield **不要**设 `HYPERRAG_TEST_IMAGE` 去拉 ACR hyperrag-test。

### 4.5 栈 05-actio（可选）

- 无 Actio：secrets 可 placeholder，视频能力受限（与 viknow2 minimal env 类似）。
- 有 Actio：与 platform **同一网络**，`ACTIO_SERVER_URL=http://<service>:8600`；API key 在 secrets。

**单机私有化** 一般 **不需要** 236↔207 的 `storage-proxy-207`（那是跨机 grant URL）。Actio 与 storage 同网时，`STORAGE_DOWNLOAD_BASE_URL` 可设为浏览器可达的 storage 地址或内网 `http://storage:8080`（按 Actio 能否访问 storage 调整）。

---

## 5. Postgres 一次性初始化

在 01 栈 Postgres 容器执行（密码与用户与 `.env` 一致）：

```sql
CREATE DATABASE viknow_access OWNER viknow_ops;
CREATE DATABASE viknow_agent OWNER viknow_ops;
CREATE DATABASE viknow_knowledge OWNER viknow_ops;
CREATE DATABASE viknow_storage OWNER viknow_ops;
```

storage 库表结构：viknow-storage 仓 `bash deploy/init-storage-database.sh`（把 `VIKNOW_OPS_PG_CONTAINER` 指到你的 postgres 容器名）。

业务库表：platform 启动时多数由代码 `store._ensure()` 创建；见 `docs/knowledge-index-persistence.md`。

---

## 6. Ops 表（Redis / Neo4j / S3） bootstrap

Greenfield 在 **03 首次启动前**，在能访问 `VIKNOW_OPS_DSN` 的环境执行（236 惯例）：

1. 准备宿主机文件（不提交）：
   - `pg-demo.env` — 三个业务 DSN + ops DSN（见 platform 仓 `deploy/236/env/pg-demo.env.example`）
   - `deploy.env` — 模型与 **Redis/Neo4j 密码**（repair 会读；**不会**进 platform 容器 env）
2. 在 **platform 源码树** 运行：

```bash
bash deploy/236/env/refresh-secrets-from-host.sh   # 可选：从宿主机合并 secrets.env
bash scripts/repair-ops-infra-secrets-from-host.sh
```

1Panel 客户若无 SSH，可临时 `docker run --rm --network viknow-platform_data` 挂载源码 + env 跑上述脚本，或运维笔记本连 VPN 执行一次。

---

## 7. 1Panel 操作注意

| 问题 | 处理 |
|------|------|
| `${VIKNOW_PLATFORM_IMAGE}` 未替换 | 编排目录 **`.env`** 必须设镜像 tag，不能只改 env_file |
| 改 `secrets.env` 不生效 | **强制重建** platform 容器，不要仅重启 |
| Playground 白屏 / CORS | `VIKNOW_CORS_ORIGINS` 与浏览器访问 UI 的 Origin 完全一致 |
| 上传库失败 | 检查 `STORAGE_CORS_ORIGINS` 与 `VIKNOW_STORAGE_URL`（浏览器）、token 一致 |
| 索引失败缺 Redis/Neo4j | 未跑 repair / ops 表空 — 见 §6 |
| ASR / 视频 | Actio + 模型栈是否可达；Whisper 依赖见 viknow2 运维笔记 |

---

## 8. 冒烟

```bash
curl -sf http://<IP>:5198/playground
curl -sf http://<IP>:5218/docs
curl -sf -H "Authorization: Bearer <VIKNOW_STORAGE_TOKEN>" http://<IP>:5219/health
```

Playground 登录使用 **`VIKNOW_APP_KEY`**（`X-App-Key`）；storage 用 **Bearer `VIKNOW_STORAGE_TOKEN`**。

---

## 9. 离线镜像

在可联网机构机：

1. `viknow-platform`：`docker build -f deploy/236/Dockerfile -t viknow-platform-playground:<tag> .`（先 `prepare-build-context.sh`）
2. `viknow-storage`：`docker compose build` 或 ACR 已推送 tag
3. 01 栈中间件镜像 `docker save` → 客户 `docker load`

1Panel **不会**替你做 load；在宿主机终端 load 完后，面板里只选本地 tag。

---

## 10. 与 viknow2-jinhe 并存

| | **Platform（本指南）** | **viknow2-jinhe** |
|--|------------------------|-------------------|
| 镜像 | `viknow-platform-playground` | `viknow2-app` |
| 存储 | viknow-storage + ops PG | JuiceFS + MinIO + 业务 PG |
| 端口 | 5198 / 5218 / 5219 | 通常 8000 |
| 1Panel | 多栈 + 三文件 env | 见 [`../viknow2-jinhe`](../viknow2-jinhe/) |

同一客户 **可以** 两套并存，但 **不要** 共用 Redis db0 / 同一 PG schema  unless 明确规划。

---

模板 compose：[`stacks-1panel/`](stacks-1panel/)  
更新：2026-11-03
