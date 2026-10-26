# 会话

会话是用户和一个 agent 之间的一串问答。外壳把用户能看见的问答放在 `run_events`。DeepAgents 把下一轮还要用的上下文放在自己的 checkpoint。这两份记录不是同一件事。

你可以用会话做这些事：

- 为一个用户和某个 agent 打开会话，并在同一会话上继续下一轮。
- 回放这个会话里已经写完的用户消息和助手正文。
- 在没有正在跑的 run 时删掉会话及其 run、事件和文件。

## 核心概念

**Session。** `VIKNOW_ACCESS_DSN` 的 `sessions` 行。字段包括 `user_id`、`agent_id`、`title`、`workspace_id`、`library_ids`。第一轮写入的 `library_ids` 会留在这行上。继续会话的请求即使再带 `library_ids`，也不会改这组库。

**Workspace。** 每个新会话建一行 `workspaces`。`workspace_id` 会进 checkpoint 的 thread id。删除会话时这行一起软删。

**Run。** `run_refs` 里的一轮。状态至少有 `running`，结束时由终态事件同一事务写成 `completed`、`failed` 或 `interrupted`。`public()` 返回 id、session、input、model、file_ids、status、output、citations、token_usage 和时间。

**用户可见的历史。** `Store.session_turns` 只读 `run_events` 里 `payload.session_id` 匹配、类型为 `user_message` 或 `message_completed` 的行，按写入顺序排列。它不扫描 checkpoint。

**Runtime 上下文。** DeepAgents 的 LangGraph checkpoint，库是 `VIKNOW_AGENT_DSN`。thread id 由 runtime 拼成 `{user_id}:{workspace_id 或 default}:{session_id}`。删会话不会删这些 checkpoint 行。会话已经不可达，新的 run 不会再打开它。

## 打开和继续

`POST /api/v1/agent/agents/{agent_id}/sessions` 建会话并跑第一轮。`POST .../sessions/{session_id}/runs` 在已有会话上再跑一轮。

两条路径都要求：

- `message` 非空，否则 422。
- `user_id` 对应已存在的用户，否则 404。
- `agent_id` 是已登记的对外 id，否则 400，`code` 为 `unknown_profile`。
- 若 profile 启用了 `knowledge_search` 或 `knowledge_discover`，第一轮必须带恰好一个 `library_ids`。缺了是 422，多于一个也是 422。
- 会话上已有 `running` 的 run 时，继续或删除返回 409，`code` 为 `session_busy`。

每次提交都会把交给 runtime 的 `library_ids` grant 给该用户。继续会话时这组 id 来自会话行，不是来自本次请求。`host` 再滤一遍：只把本次上下文里、且该用户已被授予的库放进 `AgentTurnInput`。

## 删除

`DELETE /api/v1/agent/agents/{agent_id}/sessions/{session_id}?user_id=` 要求 `user_id`。会话不存在、agent 不匹配或用户不匹配时 404。有正在跑的 run 时 409。成功时同一事务给 session、workspace、file、run、event、run_file 写同一个 `deleted_at`，响应 204，没有响应体。

`DELETE /api/v1/users/{user_id}/sessions/{session_id}` 走同一套删除。`DELETE /api/v1/users/{user_id}` 删该用户名下的会话。

单轮删除是 `DELETE .../runs/{run_id}`。该轮仍是 `running` 时 409，`code` 为 `run_busy`。

## 中断

`POST .../runs/{run_id}/interrupt` 取消 agent loop 上的任务。run 已经不在 `running` 时 409，`code` 为 `run_not_running`。编排器最多等 2 秒。任务没有自己结束时，由编排器把 run 收成中断。

下一轮开始时，runtime 读取这条 thread 的 checkpoint。里面若还有没有 tool 结果的调用，会先补上内容为 `tool call did not finish (run interrupted)` 的 tool 消息，再送出新的用户消息。thread id 仍是 `{user_id}:{workspace_id 或 default}:{session_id}`。

进程再次启动时，`close_lost_runs` 把库里仍是 `running`、而本进程没有任务的 run 写成失败，事件 `code` 为 `run_lost`。

## 下一步

- [事件](events.md)：SSE 和历史读哪些行。
- [HTTP](http.md)：完整路径。
- [接入层数据](../data/access.md)：表结构。
