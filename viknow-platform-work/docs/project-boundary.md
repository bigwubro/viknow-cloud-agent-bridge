# viknow-platform 项目边界

**viknow-platform 仓库只承载 ViKnow Platform 接入层 + Playground UI + agent/HyperRAG 集成代码。**  
**数据库、Redis、Neo4j 等由外部任务单独 provision；viknow-platform 只连 env/ops，不在本仓启动基础设施。**

详见 [`docs/external-infra.md`](external-infra.md)。

## 在本仓之内

| 范围 | 路径 / 进程 |
|------|-------------|
| 接入层 API | `src/viknow_platform/`、`python -m viknow_platform` |
| Playground UI | `ui/`、`python -m ui.server` |
| Playground 236 部署 | `deploy/236/`（应用容器与 env 文件） |
| 运维配置 | `VIKNOW_OPS_DSN` → ops 表（连接信息由 **外部 PG 上的 viknow_ops** 提供） |

## 在本仓之外

| 不属于 viknow-platform 本仓 |
|---------------------|
| **起 Postgres / Redis / Neo4j 的 compose**（已移除 `deploy/viknow-platform-dev-data/`） |
| viknow2-test、飞书、跳板仓、宿主机 `deploy.env` 全量进 Git |
| 在 `config/app.yaml` 堆未实现能力 |

## 部署线（应用 only）

```text
deploy/236/   → viknow-platform-playground（5198 UI + 5218 API）
```

## 新增配置 checklist

1. 是否被 `src/viknow-platform` 或 `ui/` 读取？  
2. Playground 236 必需 → `api.env` / `ui.env` / `secrets.env` + `ENV.md`。  
3. 基础设施连接 → 只写 DSN/ops，**不加** compose 或 init 脚本进本仓。
