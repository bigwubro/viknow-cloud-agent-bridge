# Qwen3.6 两路 1P+1D（实验）

对外仍是 `127.0.0.1:8500`。不碰 `viknow2-test`。

- 实例 A：0 卡预填充 `:8510`，1 卡出词 `:8511`，对内代理 `:8520`
- 实例 B：2 卡预填充 `:8512`，3 卡出词 `:8513`，对内代理 `:8521`
- nginx 在 `:8500` 对两路 `least_conn`

启动会停掉现网 `vllm-vlm`（四卡混跑）。回滚：`./stop-pd-restore-dp4.sh`。

同一套 B 对照见 `loadtests/workload-b-vs-sf/RESULTS_PD.md`。出词侧不要开 MTP：Mamba 块会对不齐，回包乱码。

出词卡慢的主因不是 184 token 的 decode，而是 NIXL 从预填充卡 READ 约 246MB KV。`UCX_TLS=all` + `UCX_NET_DEVICES=all` 只跑出约 330MB/s，40 路并发时每条传输被均分到 30 秒以上，出词卡日志长期是 `Running: 0 / Waiting=Deferred`。现网改成同机 CUDA IPC（`cuda_ipc,cuda_copy,gdr_copy,sm,self`），出词侧用 FCFS，NIXL `num_threads=8`。
