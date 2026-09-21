# Qwen3.6 Grafana

`qwen36-3p1d-dashboard.json` 对齐原先「vLLM · Qwen3.6-35B · 一个实例」（uid `b281712d-…`）。

现网是 3P+1D，同一张 dashboard，图按 **P / D** 左右拆开。数据源 `prometheus-qwen36`（:9108），只取 `:8510/:8511/:8512` 预填充和 `:8513` 出词，不含 `:8500`。

`prometheus-qwen36.yml` 把 `:8511` 从旧 1P+1D 的 `role=decode / qwen36-d1` 改成 `role=prefill / qwen36-p1`。拷到 236 `services/qwen36-dcgm-gpu0-3/prometheus.yml` 后 reload sidecar。

**P 调度（p0/p1/p2）** 与 embedding 同理：瞬时 scrape 常把 prefill 的 running 打成 0；面板用 `max_over_time(...[10s])` + 右轴 QPS。P 上 running 只有「正在 prefill」的请求，量级通常是每卡个位数，不能和 D 的几十条 decode 比。

导入：

```bash
python3 grafana/build_qwen36_pd_dashboard.py
# 在 236 上
python3 grafana/apply_qwen36_pd_dashboard.py
```
