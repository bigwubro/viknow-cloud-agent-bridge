# 配置

平台配置和某一轮的循环参数是分开的。模型名、provider 地址和密钥的环境变量名在 `viknow_ops`。多个 agent 共用的循环参数在 `src/agents/runtime/runtime.yaml`。某个 agent 的工具、技能和提示词在 `src/agents/<agent>/profile/*.yaml`。

agent runtime 不读 `app.yaml`。

## 三份来源

| 内容 | 来源 | 谁读取 |
| --- | --- | --- |
| provider、模型名 | `viknow_ops` | `build_agent_app_config` |
| 温度、超时、沙箱、递归上限、checkpoint 开关 | `runtime.yaml` | `load_kernel_config`，再由外壳改 checkpoint |
| 工具、技能、系统提示、回答检查 | profile YAML | 适配器按 agent 包装配 |
| checkpoint 连接 | `VIKNOW_AGENT_DSN` | 外壳写入 AppConfig，runtime 自己建 saver |
| 状态目录 | 外壳选定的 `files_root/state` | 工具恢复日志写到其下 `traces/` |

`runtime.yaml` 的 `model` 块没有 URL，也没有密钥。外壳把它和 ops 里的模型引用合成 `AgentModelConfig`。

checkpoint 在这条路径上被固定为启用、后端 Postgres、DSN 来自 `VIKNOW_AGENT_DSN`。

## 运维接口

`/api/v1/om` 要管理员凭证，不要 app key。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/v1/om/config` | 读出可配置项 |
| POST | `/api/v1/om/config` | body 含 `table`、`column`、`value`，可选 `id` |
| GET | `/api/v1/om/config/changes` | 变更记录 |
| POST | `/api/v1/om/config/changes/{change_id}/revert` | 回滚一条变更 |

ops 库使用 `VIKNOW_OPS_DSN`。未设置时相关调用返回 503，`code` 为 `ops_dsn_missing`。密钥材料使用 `VIKNOW_OPS_SECRET`。文档和提交里不写这两项的值。

非法列、非法值、缺少 id 返回 400，对应 `column_not_configurable`、`invalid_value`、`row_id_required` 等 `code`。

`GET /api/v1/om/config` 的正文是 ops 库的表，键包括 `model_provider`、`model`、`agent_model`，以及 `observe_setting` 这类单行配置。没有 `switches`，也没有备份接口。

## 下一步

- [工具和模型](../agent/tools.md)
- [Runtime](../agent/runtime.md)
- [概述](../overview.md)
