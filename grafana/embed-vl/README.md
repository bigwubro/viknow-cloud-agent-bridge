# Embedding vLLM scheduler panels

Grafana 8091 上这三张看板的 `Scheduler: running / waiting` 已重画：

- `vllm-embed-vl-8601` · :8601
- `vllm-embed-vl-8611` · :8611
- `vllm-embed-vl-8612` · :8612

瞬时 `vllm:num_requests_running` / `vllm:num_requests_waiting` 在 embedding 上经常是 0（请求短于 Prometheus scrape），旧面板看起来像空的。

新面板：

- 左轴绿色 running、红色 waiting，`or vector(0)` 保证永远有线
- 右轴虚线 QPS，对照真实流量
- 已写入 236 Grafana DB，并放在本目录
