# 事件

事件是一轮调用写给调用方的记录。每一行在 `run_events`，主键是 `(run_id, sequence)`。SSE 和历史读的是这张表。runtime 内部还有自己的事件类型；外壳只留下下面这些。

你可以用事件做这些事：

- 用 `live=true` 的 SSE 跟上正在跑的一轮。
- 用 `live=false` 或 history 读已经落库的事件。
- 从 `user_message` 和 `message_completed` 拼出这个会话的问答。

## 谁写

`host` 在调用适配器之前写开头两件：

| 类型 | 何时 | payload 里有什么 |
| --- | --- | --- |
| `run_started` | `RunEvents.started` | `session_id`、`library_ids` |
| `user_message` | 紧接着 | `session_id`、`text`、`attachments` |

适配器随后把 DeepAgents 的事件映射出去：

| 外壳类型 | 来自 runtime | 说明 |
| --- | --- | --- |
| `message_delta` | 正文增量 | `text` |
| `model_call_started` | 模型调用开始 | 无额外业务字段 |
| `tool_call_started` | 工具开始 | `tool_call_id`、`name`、`arguments` |
| `tool_call_completed` | 工具结束或失败 | `tool_call_id`、`name`、`text`。失败时还有 `is_error: true` |
| `answer_review_started` | 回答检查开始 | runtime payload |
| `answer_review_completed` | 回答检查结束 | runtime payload。`passed` 不为真时，Playground 会显示检查未通过 |
| `message_completed` | run 成功收尾 | `text`、`citations`、`token_usage` |
| `run_completed` | 同上 | 与 run 的 status、output、citations、token_usage 同一事务 |
| `run_failed` | 失败 | `message`，有时有 `code` |
| `run_interrupted` | 中断 | 可带未完成正文 |

runtime 自己的 run-started 和 user-message 不会再写一遍。终态只接受第一次。`answer_review_*`、`tool_gate_blocked` 和补写的 `message_completed` 可以出现在终态之后。

## 怎么读

`GET /api/v1/agent/runs/{run_id}/events` 和嵌套的 `.../sessions/{session_id}/runs/{run_id}/events` 都是 SSE。查询参数：

| 参数 | 缺省 | 作用 |
| --- | --- | --- |
| `after_sequence` | 0 | 只返回 sequence 更大的事件 |
| `live` | true | true 时跟上未结束的 run；false 时只返回已经落库的事件 |

每一帧是 `event: <type>` 加一行 `data:` JSON。

`GET .../history` 在 run 仍是 `running` 时返回 409，`detail` 为 `run_not_finished`。

会话级问答不要自己扫 `run_refs`。用 `session_turns` 的规则：未删除、`payload.session_id` 等于该会话、类型是 `user_message` 或 `message_completed`，按 `created_at, sequence` 排序。助手正文在 `payload.text`。

## 下一步

- [会话](sessions.md)
- [Runtime](runtime.md)
- [HTTP](http.md)
