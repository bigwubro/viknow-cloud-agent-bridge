# Qwen3.6 PD 分离（实验）

对外仍是 `127.0.0.1:8500`。不碰 `viknow2-test`。

两路 1P+1D：`./run-pd-1p1d.sh`

- 实例 A：0 卡预填充 `:8510`，1 卡出词 `:8511`，对内代理 `:8520`
- 实例 B：2 卡预填充 `:8512`，3 卡出词 `:8513`，对内代理 `:8521`
- nginx 在 `:8500` 对两路 `least_conn`

3P+1D：`./run-pd-3p1d.sh`

- 0/1/2 卡预填充 `:8510` / `:8511` / `:8512`
- 3 卡出词 `:8513`
- 代理 `:8520` 对三路预填充 least-inflight，出词固定一台
- 四卡互相可见，保证 NIXL CUDA IPC
- P/D 都开 MTP-1（`--speculative-config method=mtp,num_speculative_tokens=1`），避免只在出词侧开导致块大小对不齐

启动会停掉现网 `vllm-vlm`（四卡混跑）。回滚：`./stop-pd-restore-dp4.sh`。

同一套 B 对照见 `loadtests/workload-b-vs-sf/RESULTS_PD.md`。出词侧不要开 MTP：Mamba 块会对不齐，回包乱码。

出词卡慢的主因是 NIXL 传约 246MB KV。现网仍是 `NixlConnector` pull/`ucp_get`。无 NVLink 时 UCX 默认关掉 `cuda_ipc` GET，数据面会落到 `sysv` 软件模拟（约 700MB/s）。启动项打开 `UCX_CUDA_IPC_ENABLE_GET_ZCOPY=on` 和 `UCX_CUDA_IPC_BW=50000MBs`，让 CUDA→CUDA get 走 `cuda_ipc`。预填充侧 `--max-num-batched-tokens 32768`（出词仍 16384），`max-num-partial-prefills` 保持 1。试过 `NixlPushConnector` WRITE，短请求会挂死，已退回 pull。`sitecustomize.py` 设 `ucx_error_handling_mode=none`。只占用 GPU 0–3 与 `:8500`，不碰其他已有容器。
