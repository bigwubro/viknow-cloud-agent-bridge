# Qwen3-VL-Embedding-2B · DP3 · :8601

把 **`vllm-embed-vl` / `vllm-embed-vl-g5` / `vllm-embed-vl-g7-a`**（:8601 / :8611 / :8612）收成 **一个 vLLM 进程，`--data-parallel-size 3`**，对外仍只暴露 **:8601**。模式与现网 **`vllm-rerank-vl`**（`--data-parallel-size 2` + `--api-server-count 1`）一致。

## 会动什么 / 不会动什么

| 操作 | 容器 |
|------|------|
| **删除并替换** | `vllm-embed-vl`, `vllm-embed-vl-g5`, `vllm-embed-vl-g7-a` |
| **不动** | `vllm-embed` (:8502)、`vllm-rerank-vl` (:8602)、whisper、Qwen PD、viknow 等 |

## GPU

默认 **5,6,7**（与原三路一卡一实例相同）。新容器名默认仍为 **`vllm-embed-vl`**，便于 `VIKNOW_EMBEDDING_BASE_URL=http://ai.qingxiang.tech:8601/v1` 不变。

## GPU7 与 `vllm-embed` 冲突（重要）

原 **g7-a** 与 **`vllm-embed`（0.6B 文本）** 都曾占 **GPU7**。DP3 第三路也要用 **GPU7** 时，**`vllm-embed` 若仍绑在 7 上，可能显存争用或 OOM**。脚本默认 **检测后退出**；若你已确认可共存或已把 `vllm-embed` 挪到别的卡，可：

```bash
ALLOW_GPU7_SHARE=1 ./recreate-embed-vl-dp3.sh
```

更稳妥：先把 **`vllm-embed` 挪到空闲 GPU**（例如 `services/vllm-embed-whisper-gpu/recreate.sh` 里 `EMBED_GPU`），再跑本脚本——这属于改 embed 容器，但不影响 rerank/PD。

## 在 236 上执行

```bash
cd /path/to/recreate-embed-vl-dp3.sh
chmod +x recreate-embed-vl-dp3.sh
# 可选：export EMBED_VL_API_KEY=...  否则从现有 vllm-embed-vl 启动参数读取
./recreate-embed-vl-dp3.sh
```

## 监控

Grafana 上原三张 **8611/8612** 看板可下线或合并为单实例 **:8601**；Prometheus `instance` 只剩一个 target。

## 回滚

保留 compose/旧 run 命令，重新 `docker compose up` 三个 service，或从备份的 `docker inspect` 启动参数恢复三路单卡。
