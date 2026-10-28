# 接入层数据

接入层的业务行在 `VIKNOW_ACCESS_DSN` 指向的库 `viknow3_access`，schema `access`。表由 `viknow3.store` 在进程使用存储时创建。删除是写 `deleted_at`，不是物理删除。

DeepAgents 的 checkpoint 在 `VIKNOW_AGENT_DSN`（`viknow3_agent`，schema `checkpoint`）。ADK 的 session 在同一个库的 schema `adk`。知识库在 `VIKNOW_HYPERRAG_DSN`（`viknow3_knowledge`）：目录在 schema `catalog`，HyperRAG 表在 `hyperrag`，向量在 `hyperrag_kg`。运维配置在 `viknow3_ops` 的 schema `ops`。`public` 只放扩展。

## 表

| 表 | 主键 | 记什么 |
| --- | --- | --- |
| `users` | `id` | `display_name`、`memory_enabled`、时间、`deleted_at` |
| `workspaces` | `id` | 属于哪个 `session_id` |
| `sessions` | `id` | `user_id`、`agent_id`、`title`、`workspace_id`、`library_ids` |
| `files` | `id` | workspace、session、agent、文件名、mime、大小、`kind`、`status`、字节内容 |
| `run_refs` | `id` | 一轮的 session、user、agent、`file_ids`、`status`、`input`、`output`、`model`、`citations`、`token_usage` |
| `run_events` | `(run_id, sequence)` | `type`、`payload`、时间、`deleted_at` |
| `run_files` | `(run_id, file_id)` | 这一轮用到的文件 |

`sessions.library_ids` 和 `run_refs` 的 `file_ids`、`citations`、`token_usage` 是 JSON。`files.content` 是 `bytea`。

对外 JSON 不返回 `deleted_at`，也不返回文件字节。用户的 `public()` 含 `id`、`display_name`、时间、`memory_enabled`。会话的 `public()` 含 `id`、`user_id`、`agent_id`、`title`、`library_ids` 和时间，不含 `workspace_id`。

## 谁写终态

一轮成功、失败或中断时，终态事件和 `run_refs` 上的 `status`、`output`、`citations`、`token_usage` 在同一次写入里完成。先改 run 的字段，再插入终态事件。读到终态事件的一方能看到最终字段。

`run_events.payload` 里会带 `session_id`。会话问答按这个字段过滤，不按 `run_refs` 再join 出正文。

## 软删范围

删会话时，同一事务更新 `sessions`、`workspaces`、`files`、`run_refs`、`run_events`、`run_files` 的 `deleted_at`。`run_refs` 的软删列也是 `deleted_at`。checkpoint 表不在这次事务里。

## 下一步

- [会话](../agent/sessions.md)
- [事件](../agent/events.md)
- [Agent HTTP](../agent/http.md)
