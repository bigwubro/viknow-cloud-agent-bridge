# Qwen3.6 两路 1P+1D（实验）

对外仍是 `127.0.0.1:8500`。不碰 `viknow2-test`。

- 实例 A：0 卡预填充 `:8510`，1 卡出词 `:8511`，对内代理 `:8520`
- 实例 B：2 卡预填充 `:8512`，3 卡出词 `:8513`，对内代理 `:8521`
- nginx 在 `:8500` 对两路 `least_conn`

启动会停掉现网 `vllm-vlm`（四卡混跑）。回滚：`./stop-pd-restore-dp4.sh`。

同一套 B 对照见 `loadtests/workload-b-vs-sf/RESULTS_PD.md`。出词侧不要开 MTP：Mamba 块会对不齐，回包乱码。

出词卡慢的主因不是 184 token 的 decode，而是 NIXL 从预填充卡 READ 约 246MB KV。只改 `UCX_TLS` 不够：主机 `yama ptrace_scope=1`，容器默认没有 `CAP_SYS_PTRACE`，UCX 的 `cuda_ipc` 会静默退回 `cuda_copy+tcp`（约 330MB/s）。这台机器的 NVIDIA runtime 只允许 `compute,utility`，不能再加 `ipc` 能力。现网同一对 P/D 都能看见两张卡，并加上 `SYS_PTRACE` 和 `seccomp=unconfined`。
