# viknow3 项目边界

**viknow3 仓库只承载 ViKnow 3 接入层 + Playground + 本仓库 agent/HyperRAG 集成。** 不属于 viknow3 的职责不要放进本仓代码、配置或部署脚本。

## 在本仓之内

| 范围 | 路径 / 进程 | 说明 |
|------|-------------|------|
| 接入层 API | `src/viknow3/access/`、`python -m viknow3` | HTTP `/api/v1/*`、会话、知识库、Agent |
| 应用 / 运行时 | `src/viknow3/service/`、`application/`（若存在） | HyperRAG、Actio、Agent 装配；**不**实现 Postgres/S3 协议本身 |
| Playground UI | `ui/`、`python -m ui.server` | 静态页 + `runtime-config.js`；**不** mount 接入层 |
| Playground 236 部署 | `deploy/236/` | `ui.env` / `api.env` / `secrets.env`，见 `deploy/236/env/ENV.md` |
| 本仓开发依赖栈 | `deploy/viknow3-dev-data/` | viknow3 专用 PG/Redis/Neo4j（与 viknow2-test **端口隔离**） |
| Demo 接入层容器 | `deploy/demo/` | 可选：`viknow3-demo` `:5197`，与 Playground 容器 **并行**，仍属 viknow3 二进制 |
| 运维配置来源 | `VIKNOW_OPS_DSN` → `viknow_ops` 表 | S3/模型连接等；**不在仓内维护** `hyperrag.yml` 给人改 |

## 在本仓之外（不要塞进 viknow3 配置）

| 不属于 viknow3 | 说明 |
|----------------|------|
| **viknow2-test** / viknow2 应用栈 | 另一产品；236 上 `:5175` 等仅文档对比，不写入 viknow3 env |
| **飞书知识库同步** | 无接入层实现；**不**在 `secrets.env` / `app.yaml` 保留 FEISHU_* |
| **宿主机 `deploy.env` 全量** | 236 共享运维文件；Playground 只用 `refresh-secrets-from-host.sh` 合并后 **再 strip** 非 viknow3 键 |
| **Cursor 跳板仓** `viknow-cloud-agent-bridge` | 镜像/Agent 说明；业务以 Gitea `castmeta-research/viknow3` 为准 |

## 三条部署线（勿混）

```text
deploy/236/          → viknow3-playground（5198 UI + 5218 API）  ← 产品 Playground
deploy/demo/         → viknow3-demo（5197 单进程接入层）       ← 旧 demo，可单独 redeploy
deploy/viknow3-dev-data/ → 开发用 PG/Redis/Neo4j              ← 基础设施，非应用镜像
```

## `config/app.yaml`

仅 **部分** 被 viknow3 读取（如 `share.agent.model_ref`、`agent.deepagents.workspace`）。  
未在 `src/viknow3` 引用的段落视为 **历史遗留**，新增能力 **不得** 再往里堆；优先 env + ops 表。

## 新增配置 checklist

1. 是否被 `src/viknow3` 或 `ui/` 读取？否 → 不进仓。  
2. 是否 Playground 236 必需？是 → 进 `api.env` / `ui.env` / `secrets.env` 之一，并更新 `ENV.md`。  
3. 是否仅 236 宿主机共享？是 → 只出现在 refresh 合并源，且应在 strip 列表中剔除。
