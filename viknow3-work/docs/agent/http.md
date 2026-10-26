# Agent HTTP

Agent 的 HTTP 都在 `/api/v1/agent`，由 `access/agent/routes.py` 挂出。除特别写出的接口外，都要 app key。列表接口的 `limit` 缺省 20，夹在 1 和 100 之间。

JSON 和 `multipart/form-data` 都可以提交一轮。multipart 里的文件字段会变成上传；`library_ids`、`attachment_file_ids`、`file_ids` 可以重复出现并收成列表。单个上传超过 20MB 时 400，`code` 为 `file_too_large`。

## 一轮调用要带的字段

| 字段 | 要求 |
| --- | --- |
| `message` | 必填，空白是 422 |
| `user_id` | 必填，用户必须已经存在 |
| `library_ids` | 仅第一轮使用。启用了知识库工具的 agent 必须恰好一个库 |
| `agent_model` 或 `model` | 可选。未知模型 400，`code` 为 `unknown_model` |
| `attachment_file_ids` 或 `file_ids` | 可选。文件必须属于这个会话的 workspace，否则 400，`code` 为 `invalid_file_ids` |
| `profile_id` | 只在 `POST /runs` 上使用。缺省是 app key 的默认 agent |
| `session_id` | 只在 `POST /runs` 上使用。空则新建会话 |

`POST /agents/{agent_id}/sessions` 的成功体包含 `run_id`、`session_id`、`workspace_id`、`agent_id`、`uploaded_file_ids`。

`POST /runs` 的成功体包含 `run_id`、`session_id`、`workspace_id`、`profile_id`、`status`、`file_ids`。

## 路径

| 方法 | 路径 | 结果 |
| --- | --- | --- |
| GET | `/agents` | agent 列表 |
| GET | `/agents/{agent_id}` | 一个 agent。未知是 404，`detail` 为 `agent not found` |
| POST | `/agents/{agent_id}/sessions` | 新会话和第一轮 |
| GET | `/agents/{agent_id}/sessions?user_id=` | 该用户在这个 agent 下的会话 |
| GET | `/agents/{agent_id}/sessions/{session_id}` | 一个会话 |
| PATCH | `/agents/{agent_id}/sessions/{session_id}` | 用 body 里的 `title` 改标题 |
| DELETE | `/agents/{agent_id}/sessions/{session_id}?user_id=` | 204。忙则 409 `session_busy` |
| POST | `/agents/{agent_id}/sessions/{session_id}/runs` | 继续会话 |
| GET | `/agents/{agent_id}/sessions/{session_id}/runs` | 该会话的 run。可带 `status`、`limit`、`after` |
| GET | `/agents/{agent_id}/sessions/{session_id}/runs/{run_id}` | 一个 run |
| DELETE | `/agents/{agent_id}/sessions/{session_id}/runs/{run_id}` | 204。忙则 409 `run_busy` |
| GET | `/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/events` | SSE |
| GET | `/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/history` | 已结束 run 的事件。仍在跑则 409 |
| POST | `/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/interrupt` | `{run_id, session_id}` |
| GET | `/agents/{agent_id}/sessions/{session_id}/files` | 会话文件 |
| DELETE | `/agents/{agent_id}/sessions/{session_id}/files/{file_id}` | 204 |
| GET | `/profiles` | `{default_profile, profiles}` |
| GET | `/profiles/{agent_id}` | 同单个 agent |
| GET | `/models` | `default_model` 和 `models` |
| GET | `/permissions?user_id=` | 还要 `library_id` 或 `asset_id`。返回 `{allowed, user_id, library_id, asset_id}` |
| POST | `/runs` | 用 `profile_id` 开始一轮，可带已有 `session_id` |
| GET | `/runs/{run_id}` | 一个 run |
| GET | `/runs/{run_id}/events` | SSE |
| POST | `/runs/{run_id}/interrupt` | `{status: interrupted}` |
| GET | `/sessions?user_id=` | 可带 `agent_id`，缺省用 app key 的默认 agent |
| GET | `/sessions/{session_id}` | 一个会话 |
| PATCH | `/sessions/{session_id}` | 改标题 |
| GET | `/sessions/{session_id}/runs` | 该会话的 run |
| GET | `/sessions/{session_id}/workspace` | workspace |
| GET | `/sessions/{session_id}/files` | 文件列表 |
| POST | `/sessions/{session_id}/files` | 上传。没有文件时 422 |
| GET | `/files/{file_id}` | 文件元数据 |
| GET | `/files/{file_id}/content` | 文件字节 |
| DELETE | `/files/{file_id}` | 204。仍被 run 使用时 409，`code` 为 `file_in_use` |

嵌套路径会核对 run 属于该 session 和 agent。对不上按不存在处理。

## 错误

| HTTP | code 或 detail | 何时 |
| --- | --- | --- |
| 401 | `invalid_app_key` | app key 缺失或无效 |
| 400 | `unknown_profile` | agent 未登记 |
| 400 | `unknown_model` | 模型名不在已知列表 |
| 400 | `invalid_file_ids` | 附件不属于这个会话 |
| 400 | `file_too_large` | 超过 20MB |
| 404 | `agent not found` / `user not found` / `session not found` / `run not found` / `file not found` | 对象不存在或不属于这次查询 |
| 409 | `session_busy` | 会话已有 running run |
| 409 | `run_busy` | 要删的 run 还在跑 |
| 409 | `run_not_running` | 中断一个已经结束的 run |
| 409 | `run_not_finished` | 对 running run 要 history |
| 409 | `file_in_use` | 文件还挂在 run 上 |
| 422 | 字段级 `detail` | `message`、`library_ids` 不合法 |

## 用户接口

`/api/v1/users` 同样要 app key。

| 方法 | 路径 | 结果 |
| --- | --- | --- |
| POST | `/api/v1/users` | body 可带 `display_name` |
| GET | `/api/v1/users` | 列表 |
| GET | `/api/v1/users/{user_id}` | 一个用户 |
| PATCH | `/api/v1/users/{user_id}` | 更新 `display_name` |
| DELETE | `/api/v1/users/{user_id}` | 204，并删除其会话 |
| DELETE | `/api/v1/users/{user_id}/sessions/{session_id}` | 204 |

## 下一步

- [会话](sessions.md)
- [事件](events.md)
- [接入层数据](../data/access.md)
