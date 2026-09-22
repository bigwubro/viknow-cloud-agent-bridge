# Embedding vLLM · Grafana（DP3 · :8601）

236 上 **Qwen3-VL-Embedding-2B** 已收成 **单容器 DP3**（`vllm-embed-vl`，GPU 5/6/7，**:8601**）。

## 看板（Grafana :8091）

三个 **UID 不变**（书签仍可用），指标均来自 **同一个** Prometheus target `127.0.0.1:8601`：

| UID | 说明 |
|-----|------|
| **`vllm-embed-vl-8601`** | 主看板：三 rank 合计 + **engine 0/1/2** running/waiting |
| **`vllm-embed-vl-8611`** | 原 :8611 / g5：Scheduler·KV 按 **engine 0（GPU5）**；E2E/QPS 仍为整实例 |
| **`vllm-embed-vl-8612`** | 原 :8612 / g7-a：Scheduler·KV 按 **engine 2（GPU7）**；E2E/QPS 仍为整实例 |

（原 :8601 单卡对应 **engine 1 / GPU6** 的专页未单独保留；总览见 8601 看板。）

## 生成与发布

```bash
python3 build_embed_vl_dashboards.py   # 从 vllm-embed-vl-8601.base.json 生成三份 JSON
python3 apply_embed_vl_grafana.py      # 在 236 上：Prometheus 只 scrape :8601，写入 Grafana DB
```

Prometheus 会去掉 **:8611 / :8612** embed target，**:8601** 打标签 `data_parallel=3`。

Scheduler 面板仍用 `instance=127.0.0.1:8601`；短请求下 running 常为 0，保留 QPS 右轴对照流量。
