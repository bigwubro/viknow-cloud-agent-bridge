# Qwen3.6 两路 1P+1D（实验）

对外仍是 `127.0.0.1:8500`。不碰 `viknow2-test`。

- 实例 A：0 卡预填充 `:8510`，1 卡出词 `:8511`，对内代理 `:8520`
- 实例 B：2 卡预填充 `:8512`，3 卡出词 `:8513`，对内代理 `:8521`
- nginx 在 `:8500` 对两路 `least_conn`

启动会停掉现网 `vllm-vlm`（四卡混跑）。回滚：`./stop-pd-restore-dp4.sh`。

同一套 B 对照见 `loadtests/workload-b-vs-sf/RESULTS_PD.md`。出词侧不要开 MTP：Mamba 块会对不齐，回包乱码。

出词卡慢的主因不是 184 token 的 decode，而是 NIXL 从预填充卡 READ 约 246MB KV。NIXL UCX 默认 `ucx_error_handling_mode=peer`，会拒掉 `sm`，只剩 `tcp` 做 AM，VRAM 就走 `cuda_copy+tcp`（约 330MB/s）。`sitecustomize.py` 把该参数改成 `none`，`UCX_TLS=sm,self,cuda_ipc,cuda_copy`（必须留 `cuda_copy` 才能注册 VRAM，不能放 `tcp`）。UCX 会加载 `cuda_ipc` lane，但 NIXL READ 是 `ucp_get`，这套 UCX 1.21 对 CUDA→CUDA get 选的是 `software emulation / sysv/memory`，无争用大约 700MB/s，不是 NVLink P2P。主机 `yama ptrace_scope=1`，容器还要 `SYS_PTRACE`、对端 GPU 可见、`/dev/nvidia-caps`。这台机器的 NVIDIA runtime 只允许 `compute,utility`。
