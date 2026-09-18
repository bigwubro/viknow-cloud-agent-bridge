# Qwen3.6 两路 1P+1D（实验）

对外仍是 `127.0.0.1:8500`。不碰 `viknow2-test`。

- 实例 A：0 卡预填充 `:8510`，1 卡出词 `:8511`，对内代理 `:8520`
- 实例 B：2 卡预填充 `:8512`，3 卡出词 `:8513`，对内代理 `:8521`
- nginx 在 `:8500` 对两路 `least_conn`

启动会停掉现网 `vllm-vlm`（四卡混跑）。回滚：`./stop-pd-restore-dp4.sh`。

同一套 B 对照见 `loadtests/workload-b-vs-sf/RESULTS_PD.md`。出词侧不要开 MTP：Mamba 块会对不齐，回包乱码。

出词卡慢的主因是 NIXL 传约 246MB KV。`NixlPushConnector` 用 WRITE/`put`，不再用 READ/`ucp_get`（后者在这套 UCX 1.21 上会落到 `sysv` 软件模拟）。`sitecustomize.py` 设 `ucx_error_handling_mode=none` 和 `num_workers=4`。`UCX_TLS=sm,self,cuda_ipc,cuda_copy`，不放 `tcp`。只占用 GPU 0–3 与 `:8500`，不碰其他已有容器。
