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
- 现网镜像：官方 `vllm/vllm-openai:v0.29.0`。自制 `vllm-viknow:0.26.0-lmcache0.5.4-mmfix-r44688` 留在磁盘，不删。
- MTP-1：P/D 同开 `method=mtp, num_speculative_tokens=1`。0.26 上长前缀会炸 D；0.29 用来验收这条是否已过。

启动会停掉现网 `vllm-vlm`（四卡混跑）。回滚：`./stop-pd-restore-dp4.sh`。

同一套 B 对照见 `loadtests/workload-b-vs-sf/RESULTS_PD.md`。0.29 上 P/D 已同开 MTP-1；0.26 长前缀会炸 D，不要退回那条。

镜像里有 LMCache 0.5.4，Qwen3.6 GDN 官方支持，但现网没开。PD 要 `MultiConnector[Nixl + LMCacheMP]`，`--chunk-size 2112` + `--separate-object-groups`；三路 P 默认仍不共享，跨 P 还要 P2P。未在本机叠过 MTP-1。

出词卡慢的主因是 NIXL 传约 246MB KV。现网仍是 `NixlConnector` pull/`ucp_get`。无 NVLink 时 UCX 默认关掉 `cuda_ipc` GET，数据面会落到 `sysv` 软件模拟（约 700MB/s）。启动项打开 `UCX_CUDA_IPC_ENABLE_GET_ZCOPY=on` 和 `UCX_CUDA_IPC_BW=50000MBs`，让 CUDA→CUDA get 走 `cuda_ipc`。预填充侧 `--max-num-batched-tokens 32768`（出词仍 16384）。0.29 已去掉 `--max-num-partial-prefills`。试过 `NixlPushConnector` WRITE，短请求会挂死，已退回 pull。`sitecustomize.py` 设 `ucx_error_handling_mode=none`。只占用 GPU 0–3 与 `:8500`，不碰其他已有容器。
