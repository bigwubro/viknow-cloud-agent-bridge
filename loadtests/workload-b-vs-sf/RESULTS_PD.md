# 两路 1P+1D 实测（同一套 B，对外仍是 :8500）

窗口：2026-09-18 11:18–12:00 UTC。  
实例 A：0 卡预填充 `:8510` + 1 卡出词 `:8511`。  
实例 B：2 卡预填充 `:8512` + 3 卡出词 `:8513`。  
nginx `least_conn` 挂在 `127.0.0.1:8500`。NIXL 传 KV。出词侧关掉 MTP，否则 Mamba 块大小和预填充侧对不齐。  
对照是同日 06:47–07:28 UTC 的四卡混跑 `DP4×TP1`（`RESULTS_LOCAL.md`）。现网现为 1P+1D，回滚用 `services/qwen36-vllm-pd/stop-pd-restore-dp4.sh`。

校准：shared 16575 + unique 13354，probe **29940**。thinking 关，`max_tokens=184`。四档全部成功，0 失败。

| 并发 | 1P+1D 成功/提交 | 每秒完成 | 中位 / 95 分位 | 混跑每秒完成 / 中位 |
|---|---|---|---|---|
| 1 | 234/234 | **0.390** | **2.54s / 2.75s** | 0.314 / 3.08s |
| 8 | 701/701 | **1.161** | **6.73s / 7.27s** | 0.840 / 9.62s |
| 32 | 896/896 | **1.465** | **23.48s / 25.76s** | 1.260 / 25.79s |
| 80 | 963/963 | 1.498 | 54.54s / 60.60s | **1.619 / 50.22s** |

并发 1 和 8：拆开更好（中位大约 0.83×、0.70×，每秒完成大约 1.24×、1.38×）。  
并发 32：略好。  
并发 80：略差。预填充只剩两张卡，80 条在途仍堆在预填充侧，`80 / 1.50 ≈ 53` 秒，和混跑「在途 / 每秒完成」同一类上限。

短请求冒烟：`1+1` 经 8500 回 `2`，约 1.35 秒。块大小未对齐时出词是乱码（预填充 2096 vs 出词 2128）。

## 出词卡为什么慢（2026-09-18 12:18 之后）

出词侧 `request_decode_time` 只有约 **1.3–1.8s**（184 token）。端到端里多出来的十几秒几乎全部是 `WAITING_FOR_REMOTE_KVS`：NIXL 从预填充卡 READ 约 **246MB** KV。出词卡日志在高压下经常是 `Running: 0`、`Waiting=Deferred`。

| 场景 | 单次 NIXL | 表观带宽（总字节/各次耗时之和） | 说明 |
|---|---|---|---|
| 无争用（并发 1 / 短请求） | 0.23–0.75s | 约 330MB/s | 直方图里约 8% 的传输落在 0.5–0.75s，条数和并发 1 的请求数对得上 |
| 并发 8（每张出词卡约 4 路） | 约 2.5–2.8s | 约 90MB/s | 4 路共享同一条约 330MB/s 的管道 |
| 并发 32（每张出词卡约 16 路） | 约 13s | 约 18MB/s | 16×246MB / 330MB/s ≈ 12s，和队列时间对齐 |
| 并发 80（每张出词卡约 40 路） | 30–40s | 约 7MB/s | 仍是同一条管道被均分，不是 decode |

Prometheus 里 `nixl_xfer_time` 的「MB/s」是 `总字节 / 各次耗时之和`，传输重叠时会被除大，不能当成墙钟带宽。墙钟聚合带宽一直在约 **330MB/s**。GPU0–3 同 NUMA、`topo -p2p r` 为 OK，但没有 NVLink。`UCX_TLS=all` + `UCX_NET_DEVICES=all` 以及后来显式加上 `cuda_ipc`、同一对容器都能看见对端 GPU，测到的仍是这条 330MB/s 路径（主机中转 + 约 80–120 个描述符）。出词侧 prefix cache 命中约 44%，但每次仍传满 246MB，没有把共享前缀减下去。

改过的启动项（`run-pd-1p1d.sh`）：出词侧 FCFS、NIXL `num_threads=8`、UCX `tcp,sm,cuda_ipc,cuda_copy,self`、同一对两张卡都可见。短请求 `1+1` 仍回 `2`（约 1.6s）。同一套 B：

| 并发 | 窗口 | 成功 | 每秒完成 | 中位 / 95 分位 | 对照（改之前 11:18 档） |
|---|---|---|---|---|---|
| 8 | 90s，12:45 UTC | 109/109 | 1.175 | 6.82s / 7.93s | 1.161 / 6.73s |
| 32 | 180s，12:48 UTC | 284/284 | 1.467 | 23.98s / 24.76s | 1.465 / 23.48s |

端到端没有压下来：出词卡仍在等 KV，不在算 184 个 token。要再压，必须提高这条跨卡拷贝的墙钟带宽（真正走通 CUDA IPC / P2P），或让出词侧少拉共享前缀（现在 246MB 一次都没减）。

## NIXL 去掉 tcp 之后（2026-09-18 13:46–13:54 UTC）

根因不是 `UCX_TLS` 列表里有没有 `cuda_ipc`。NIXL UCX 默认 `ucx_error_handling_mode=peer`，会拒掉 `sm`，只剩 `tcp` 做 AM，VRAM 就跟着 `cuda_copy+tcp` 走，墙钟大约 **330MB/s**。`sitecustomize.py` 把该参数改成 `none`，`UCX_TLS=sm,self,cuda_ipc,cuda_copy`（必须留 `cuda_copy`，否则 `registerMem` 会把 VRAM 当成 host）。

UCX worker 的 intra-node lane 是 `device(cuda_ipc/cuda)`，AM 是 `sm/sysv/cma`，**不再有 tcp**。但 NIXL 出词侧是 `ucp_get`（READ）。这套 UCX 1.21 的 `cuda_ipc` 不做 RMA get，协议表选的是：

`remote memory read ... cuda/GPU0 from cuda/dev[0]` → `0..inf | software emulation | sysv/memory`

也就是：GPU→host→sysv shm→host→GPU，不再走网卡 tcp。`get_zcopy` / `put_zcopy` 都一样。无争用大约 **680–770MB/s**（225MB / 0.30–0.35s）。还不是 NVLink/P2P 的数 GB/s，描述符仍有 50–120 个。

短请求 `1+1` 回 `2`，约 0.9–1.0s。同一套 B：

| 并发 | 窗口 | 成功 | 每秒完成 | 中位 / 95 分位 | NIXL xfer（约） | 对照（tcp 路径 12:45 档） |
|---|---|---|---|---|---|---|
| 8 | 90s，13:48 UTC | 136/136 | **1.447** | **5.42s / 6.42s** | ~1.0s | 1.175 / 6.82s，xfer ~2.6s |
| 32 | 180s，13:51 UTC | 360/360 | **1.821** | **17.72s / 18.93s** | ~6.5s | 1.467 / 23.98s，xfer ~13s |

出词侧 decode 仍是约 1.3–1.8s。省下来的是跨卡 KV，不是 184 个 token。再往上要真 P2P，得换一条不是 `ucp_get` 的路径（NIXL send/recv 或 GDR），或者减少描述符 / 少拉共享前缀。

## 80 并发目标 10s（2026-09-18 14:38 UTC）

只动 GPU 0–3 / `qwen36-*` / `:8500`。`viknow2-test`、`:8505`、`:8506`、rerank 等未停。

单卡 P 在约 13k 独特 token 上能到约 **4.2 QPS**。两路预填充理论上约 8.5 QPS，按 Little 定律 `80/8.5≈9.4s`，**只有 NIXL 不再卡死时才够**。现网 pull/`ucp_get` 仍是 `sysv` 软件模拟。试过 `NixlPushConnector` WRITE：引擎能起，但 `1+1` 挂死 60s+，已退回 pull。

同一套 B，c80 × 180s：

| 并发 | 成功 | 每秒完成 | 中位 / 95 分位 | 对照 |
|---|---|---|---|---|
| 80 | 394/394 | **1.857** | **39.98s / 66.65s** | 旧 1P+1D 1.498 / 54.54s；混跑 DP4 1.619 / 50.22s |

中位 40s，**没有到 10s**。吞吐仍被 NIXL 锁在约 1.86 QPS（`80/1.86≈43s`）。短请求 `1+1` 回 `2`（约 1.2s）。`:8500` 200。

## 20 并发（2026-09-18 14:46 UTC）

未改拓扑、未改启动项。现网仍是两路 1P+1D + NIXL pull（`ucp_get` / sysv 软件模拟）。同一套 B，c20 × 120s，`:8500` 全程 200。

| 并发 | 窗口 | 成功/提交 | 每秒完成 | 中位 / 95 分位 | Little `c/QPS` |
|---|---|---|---|---|---|
| 8 | 90s，13:48 UTC | 136/136 | 1.447 | 5.42s / 6.42s | 5.5s |
| **20** | **120s，14:46 UTC** | **222/223** | **1.691** | **11.81s / 12.57s** | **11.8s** |
| 32 | 180s，13:51 UTC | 360/360 | 1.821 | 17.72s / 18.93s | 17.6s |
| 80 | 180s，14:38 UTC | 394/394 | 1.857 | 39.98s / 66.65s | 43.1s |

按 c8–c32 线性插值，c20 中位应约 11.6s，测到 11.81s。吞吐已经到天花板的约 91%（1.69 / 1.86）。1 条失败是 HTTP 500，不是超时。出词侧日志多数时刻 `Running: 0`、`Waiting=Deferred`，仍在等远程 KV。prefix 命中约 45%，四卡平均功率约 976W。其他容器未动。

## CUDA IPC GET（2026-09-18 18:55 UTC）

无 NVLink 时 UCX 默认关掉 `cuda_ipc` GET。`NixlConnector` 是 pull/`ucp_get`，所以数据面一直是 `software emulation | sysv/memory`。现网加上 `UCX_CUDA_IPC_ENABLE_GET_ZCOPY=on` 和 `UCX_CUDA_IPC_BW=50000MBs` 后，出词侧协议表变为：

`remote memory read by ucp_get*(multi) into cuda/GPU0 from cuda/dev[0]` → `0..inf | zero-copy | cuda_ipc/cuda`

NIXL 单次仍约 258MB。出词侧累计：平均 xfer **10–11ms**（原先无争用 0.30–0.35s，高压 1–13s），表观约 **24GB/s**。c8 窗口里 115 次传输全部 <25ms。出词侧 `queue` 均值约 24ms，decode 约 1.2s；日志多数是 `Running>0`、`Waiting=0`，不再长期 `Deferred`。

短请求 `1+1` 回 `2`（约 1.1s）。同一套 B：

| 并发 | 窗口 | 成功 | 每秒完成 | 中位 / 95 分位 | 对照（sysv GET） |
|---|---|---|---|---|---|
| 8 | 90s，18:57 UTC | 227/227 | **2.453** | **3.39s / 4.25s** | 1.447 / 5.42s |
| 20 | 90s，18:59 UTC | 256/256 | **2.643** | **7.46s / 7.87s** | 1.691 / 11.81s |

KV 不再是排队主体。吞吐从约 1.86 抬到约 2.6 QPS，卡点回到两路预填充计算。`viknow2-test` 等其他容器未停。

## 预填充 batched-tokens 32768（2026-09-18 19:19 UTC）

只改两路 P：`--max-num-batched-tokens 32768`，`max-num-partial-prefills` 仍为 1。出词仍 16384。镜像未换。CUDA IPC GET 仍在（`zero-copy | cuda_ipc`）。`1+1` 回 `2`（约 1.0s）。未 OOM。

同一套 B，c20 × 90s：

| 项 | P=16384（18:59） | P=32768（19:19） |
|---|---|---|
| 成功 | 256/256 | **257/257** |
| 每秒完成 | 2.643 | **2.697** |
| 中位 / 95 分位 | 7.46s / 7.87s | **7.43s / 8.19s** |
| P Running / Waiting | 2–3 / 4–5 | **4–5 / 0–2** |
| P prompt token/s | 约 2.3–2.5 万 | 约 2.3–2.7 万 |
| P prefill 均值 | 2.13s | 4.36s |
| P queue 均值 | 1.63s | 0.54s |

调度上一步能并进更多预填充（Waiting 掉下去了），但单卡仍约 2.5 万 token/s。Workload B 每条都是大约 1.8 万未命中，并进只是把同一块算力摊开，QPS 几乎不动。出词仍约 1.17s，NIXL 约 10ms。其他容器未停。

## 3P+1D（2026-09-18 19:47 UTC）

四卡改成 GPU0/1/2 预填充、GPU3 出词。代理对三路 P least-inflight，handshake 仍走 `NixlConnector` pull。四卡互相可见。CUDA IPC GET 仍在：`ucp_get*(multi) into cuda/GPU0 from cuda/dev[0]` → `zero-copy | cuda_ipc/cuda`。镜像和 batched-tokens 未改（P 32768 / D 16384）。`1+1` 回 `1+1=2`（0.85s）。未 OOM。`viknow2-test` 等其他容器未停。

同一套 B，c20 × 90s：

| 项 | 两路 1P+1D（19:19） | 3P+1D（19:47） |
|---|---|---|
| 成功 | 257/257 | **340/340** |
| 每秒完成 | 2.697 | **3.630** |
| 中位 / 95 分位 | 7.43s / 8.19s | **5.36s / 8.03s** |
| prefix hit | 约 45% | 44.3% |
| P 分配 | 两路 | 119 / 113 / 110 |
| P prefill 均值 | 4.36s | 约 2.56s |
| P queue 均值 | 0.54s | 约 0.10s |
| D decode 均值 | 约 1.17s | 2.17s |
| D queue / NIXL | 约 20ms / 10ms | 45ms / 13ms（342 次，338 次 <25ms） |
| D KV 峰值 | — | 12.9%，Waiting=0 |

吞吐 **+35%**（3.630 / 2.697），中位 **−28%**。三张 P 理论约 1.5×（约 4.05 QPS），测到 3.63，大约拿到理想值的九成。缺的部分在单张出词卡：并发 decode 从约 10 路升到峰值 17 路，decode 从 1.17s 升到 2.17s。c20 时 D 显存不是墙。Little `20/3.630≈5.51s`，和中位 5.36s 对齐。80 并发 10s 仍不够（按 3.63 QPS 外推约 22s）。

### 单条端到端怎么拆（c20）

代理先打完 P（`max_tokens=1`），再把 `kv_transfer_params` 交给 D，两段串行。客户端 340 条：中位 **5.37s**，均值 5.41s，p95 8.03s。引擎直方图加权均值：

| 段 | 均值 | 约占客户端 5.41s |
|---|---|---|
| P 排队 | 0.10s | 2% |
| P 预填充 | 2.54s | 47% |
| P 侧其余（分词/回包） | 0.38s | 7% |
| 代理/HTTP | 约 0.10s | 2% |
| D 排队 | 0.04s | 1% |
| NIXL CUDA IPC | 0.013s（p95 24ms） | <1% |
| D 出词 184 token | 2.15s（ITL 约 11.9ms） | 40% |

P 三段 e2e 均值 3.03s + D e2e 均值 2.29s ≈ 5.32s，和客户端 5.41s 对齐。空载 `1+1` 只有 0.85s，上面这张表是 c20 热请求。

## MTP-1 试开（2026-09-18 20:09 UTC）

P/D 都加 `--speculative-config '{"method":"mtp","num_speculative_tokens":1}'`。四卡块大小都是 **2112**（关 MTP 时 2096）。短请求 `1+1` 回 `1+1=2`（冷 6.7s，热 0.14s），CUDA IPC 仍在。

同一套 B，c20：257/257 **全部 HTTP 500**。出词引擎在 NIXL pull 做 prefix-cache 对齐时断言失败：

`AssertionError: SSM can only have one local block`（`nixl/base_worker.py` `_apply_prefix_caching`）

短上下文只有一块 GDN state，长前缀（约 30k）会变成多块，0.26 的 hybrid SSM + MTP + NIXL 这条路径不支持。出词容器随后退出。已从启动项去掉 MTP，恢复无投机的 3P+1D。

## 官方 0.29.0 + MTP-1（2026-09-18 20:47 UTC）

自制镜像 `vllm-viknow:0.26.0-lmcache0.5.4-mmfix-r44688` 留在磁盘未删。现网改用 `vllm/vllm-openai:v0.29.0`，3P+1D 不变，P/D 同开 MTP-1。0.29 不认 `--max-num-partial-prefills`，已去掉。sitecustomize 仍挂着，UCX CUDA IPC GET 仍是 `zero-copy | cuda_ipc/cuda`。

短 `1+1`：冷 1.14s，热 0.12–0.14s，回 `1+1=2`。块 2112。

同一套 B，c20 × 90s：

| 镜像 | MTP | 成功/提交 | 每秒完成 | 中位 / 95 分位 |
|---|---|---|---|---|
| 0.26 自制 | 关 | 340/340 | 3.630 | 5.37s / 8.03s |
| 0.26 自制 | MTP-1 | 0/257 | — | 全 500，D 断言退出 |
| **0.29 官方** | **MTP-1** | **342/342** | **3.642** | **5.24s / 6.75s** |

出词侧 `spec_decode`：draft 36733，接受 26293，接受率 **71.6%**。D 未退出。`viknow2-test` / rerank / fast-ingest / qwen38-gpu4 未动。

0.29 实际选中、无需再开的新路径：

- Model Runner V2
- GDN decode kernel `cuda`（fused GDN MTP）
- FlashInfer 注意力，decode_backend=`xqa`，SM120
- NIXL KV layout `LBHNC`
- Triton FP8 MoE（候选里有 FlashInfer TRTLLM/CUTLASS，自动没选）

还没动、要重启才试的：`--language-model-only`（纯文本可省视觉塔）、`--moe-backend flashinfer_cutlass`（SM120 可能起不来）、`UCX_RCACHE_MAX_UNRELEASED=1024`、MTP-3。FlashInfer fused 普通 GDN decode（PR 53645）不在 0.29，且和 MTP 互斥。QPS 几乎没涨，因为 c20 仍是预填充占一半以上；MTP 主要削了 p95。

## LMCache（2026-09-19 核对，未开）

0.29 镜像里有 `lmcache 0.5.4`（`g3e11b8ed`）和 `LMCacheMPConnector`，CLI 能跑。官方 recipe 写了 Qwen3.6 GDN：`--mamba-cache-mode align`（现网已开）、`--chunk-size` = 统一块 **2112**、`--separate-object-groups`。PD 官方接法是 `MultiConnector[NixlConnector + LMCacheMPConnector]`，每个引擎自己一台 `lmcache server`；三路 P 要共用还得再加 coordinator / P2P。

现网没改 kv 连接器。P0/P1/P2 **external_prefix_cache_hits = 0**，D3 external ≈ 100%（NIXL 从 P 拉）。

没有直接叠上去：0.5.4 + vLLM≥0.26 的 hybrid 磁盘层有 #4701（可能只存 1/N 页）；MTP + connector + GDN 有 #4674 一类风险；官方还写缺 vLLM #46865 时 MultiConnector 下 offload 会静默不触发。进程内 adapter 在本镜像缺 `CudaIPCWrapper`，只能走 MP。更便宜的一步仍是按共享前缀粘到同一张 P。

## 撤掉 LMCache，代理按前缀 hash 粘 P（2026-09-19 01:32 UTC）

现网回到 `NixlConnector` only，P batched-tokens 32768，MTP-1 仍开。`qwen36-lmc-*` 已删。代理默认 `route=prefix_hash`，只 hash 请求正文前 **8192** 个字符，同一段共享头进同一张 P。

短 `1+1` 回 `1+1=2`（热 0.14s），两次都进 P0。`:8500` 200。`viknow2-test` 等未动。

同一套 B，c20 × 90s：

| 项 | 3 路 least-inflight（无 LMCache） | LMCache + 4223 | **前缀 hash（无 LMCache）** |
|---|---|---|---|
| 成功/提交 | 342/342 | 57/57 | **130/130** |
| 每秒完成 | 3.642 | 0.454 | **1.255** |
| 中位 / 95 分位 | 5.24s / 6.75s | 40.21s / 48.01s | **15.57s / 16.43s** |
| 代理 picks | 约 1/3 各 | 约 1/3 各 | **P0 2（冒烟）/ P1 0 / P2 131** |
| 接到 B 的那张 P 本地命中 | 约 38% | 37.3% | **P2 1.635M / 4.274M = 38.2%** |
| P external | 0 | 约 10% | **0** |

B 的公共头完全粘在 P2，P1 空闲。稳态 token 命中仍约 38%（2112 对齐后的共享段，独特尾巴每次都 miss）。省下的是另外两张 P 各冷打一遍 16k；代价是预填充只剩一张卡，QPS 大约是三路均分的三分之一。

## 代理改回 least-inflight（2026-09-19 02:09 UTC）

只重启 `:8520`，四路引擎未动。健康检查 `route=least_inflight`。短 `1+1` 回 `1+1=2`（0.16s）。6 路并发短请求后 `picks [3,2,2]`。

同一套 B，c20 × 90s：

| 项 | 前缀 hash | **least-inflight（刚切，P0/P1 冷）** | **再跑一轮（三张 P 已有头）** | 对照：此前 least-inflight |
|---|---|---|---|---|
| 成功/提交 | 130/130 | **284/284** | **279/279** | 342/342 |
| 每秒完成（含收尾） | 1.255 | **2.061** | **2.126** | 3.642 |
| 中位 / 95 分位 | 15.57s / 16.43s | **5.61s / 11.76s** | **5.61s / 11.75s** | 5.24s / 6.75s |
| 代理 picks | 2 / 0 / 131 | **109 / 88 / 95** | 累计 **216 / 171 / 185** | 约 1/3 |

中位回到约 5.6s。QPS 按脚本是 `完成数/墙钟`（本轮墙钟 131–138s，含窗口外收尾）；窗口内大约 279/90≈3.1。还没回到当初 3.64：出词侧排队从约 40ms 抬到约 1s，ITL 约 14ms，MTP 接受率从 72% 掉到约 47%（计数器跨了 hash 那一轮）。NIXL 仍约 17ms。`:8500` 200。其他容器未动。

根因不是 least-inflight。P1 的 NIXL 握手线程在启动时被一条 2 帧 ZMQ 消息打死（`recv_multipart` 期望 3 帧），`:5601` 没在听。D 对 `remote_port=5601` 握手失败 171 次，和 P1 picks 171 对齐。hash 那轮没打到 P1，看不出来。

## 重启 P1，恢复 :5601（2026-09-19 02:22 UTC）

只重建 `qwen36-p1`。P0/P2/D、nginx、代理未动。`sitecustomize.py` 给握手线程加了重绑：再遇到 2 帧消息会重新 listen，不再整条退出。`:5601` 重新在听。短 `1+1` 回 `1+1=2`（0.19s）。D `failed_transfers` 仍是旧值 171，本轮新失败 0。P1 `kv_expired` 0。

同一套 B，c20 × 90s：

| 项 | P1 口死掉时的 least-inflight | **P1 恢复后** | 对照：当初 0.29 MTP-1 |
|---|---|---|---|
| 成功/提交 | 279/279 | **346/346** | 342/342 |
| 每秒完成 | 2.126（墙钟 131s） | **3.666**（墙钟 94.4s） | 3.642（93.9s） |
| 中位 / 95 分位 / 最大 | 5.61s / 11.75s / 55.7s | **5.27s / 6.52s / 9.66s** | 5.24s / 6.75s / 10.2s |
| 本轮 picks 增量 | — | **119 / 113 / 115** | 约 1/3 |

10s 以上 0 条。`:8500` 200。其他容器未动。

## Mooncake Store（2026-09-19 02:51–03:17 UTC）

官方 0.29 镜像里有 `MooncakeStoreConnector` 和 `mooncake_master`。本机无 RDMA，用 embedded + TCP，每卡贡献 8GiB，master `:50051`，lease / soft-pin 30 分钟，`client_ttl=1800`。Nixl 仍管本请求 P→D。代理仍是 `least_inflight`。短 `1+1` 回 `1+1=2`。`viknow2-test` / rerank / fast-ingest / qwen38-gpu4 未动。

第一轮：D 也挂了 Mooncake。同一套 B，c20 × 90s，**0/249**，全 HTTP 500。D 在第一条长请求上断言 `Missing current block table for store request`（`store/scheduler.py:424`）。Master 仍起来：`Mem Storage 22.75 GiB / 32 GiB`，1027 个 key。三路 P 的 `external_prefix_cache_hits` 仍是 **0**。

第二轮：D 改回 Nixl-only，P 继续 MultiConnector。短 `1+1` 仍回 `2`。同一套 B：

| 项 | Nixl-only（P1 恢复后） | **Mooncake 在 P（D 已 Nixl-only）** |
|---|---|---|
| 成功/提交 | 346/346 | **222 / 30796** |
| 每秒完成 | 3.666 | **2.377**（墙钟 93.4s，含死 P0 狂打） |
| 中位 / 95 分位 | 5.27s / 6.52s | **5.30s / 6.52s**（只计 222 条成功） |
| P external hits | 0 | **0** |
| 代理 picks | 119 / 113 / 115 | **30654 / 197 / 198** |

P0 在窗口开始（03:14:15）被 Mooncake `TRANSFER_FAIL (-800)` 打死后退出：`Failed to get 1 Mooncake keys`，随后 `_handle_invalid_blocks` 对 hybrid 多组 block table 解包 `ValueError: too many values to unpack (expected 1)`。least-inflight 看见 P0 立刻失败、在途为 0，就把后续约 3 万次都打到 P0。P1/P2 的 `save_put` 也是整批失败（142/142、130/130）。池子还停在约 22.7 GiB，lookup `ExistKey` 有次数，`Get` 为 0。

活着的那两张 P + D：NIXL 约 15ms（223 次 / 3.35s），MTP 接受 16992/23658 = **71.8%**，出词排队约 34ms。成功请求的时延和 Nixl-only 同一档，**没有跨 P 命中，也没有吞吐收益**。

现网已撤 Mooncake，回 `run-pd-3p1d.sh`。脚本留着，不要再挂到 Qwen3.6 hybrid 上。

## Mooncake 回移植上游补丁后再跑（2026-09-19 03:50 UTC）

0.29.0 镜像里没有这些已合提交。`sitecustomize.py` 回移植了：

- #50388（2026-09-13 合）：hybrid `get_block_ids` 不再按一组解包
- #54643（2026-09-07 合）：MultiConnector 拒掉的 load 不再变成 save
- #54870 的跳过逻辑（#54853 的懒加载 API 没搬，太大）

#48216 仍未合，这次用 #50388 挡崩溃。D 仍只挂 Nixl。短 `1+1` 回 `1+1=2`。引擎日志确认三处补丁都打上了。

同一套 B，c20 × 90s：

| 项 | Nixl-only | Mooncake 原版 0.29 | **Mooncake + 回移植** |
|---|---|---|---|
| 成功/提交 | 346/346 | 0/249 或 222/30796 | **322/322** |
| 每秒完成 | 3.666 | 0 / 2.377 | **3.192** |
| 中位 / 95 分位 | 5.27s / 6.52s | — / 5.30s | **5.48s / 7.62s** |
| P external hits | 0 | 0 | **P0 12672 / P1 88704 / P2 76032** |
| 引擎 | 四路活 | D 或 P0 死 | **四路活，picks 115/103/106** |

Store：3 个 client（只有 P），20.76 / 24 GiB，写成功 `save_put failed_keys=0`，P0 有 `load_get` 2 次 / 428MB / 约 300ms。池子小，驱逐 56 次、63 GiB，独特尾巴把共享头挤出去，所以 external 命中相对 query 只有约 2–4%。本地 GPU prefix 仍约 38%。NIXL 324 次、合计 4.72s（约 15ms），MTP 24864/34486 = 72.1%。

崩溃修好了，跨 P 也有命中了，但 B 的 QPS 比 Nixl-only 低一档（TCP 写入约 80–460ms，早期 put 到 2s）。现网先留这一版，回退仍是 `./run-pd-3p1d.sh`。

## Mooncake 3P+1D · B · 32 并发 · 10 分钟（2026-09-19 04:11 UTC）

现网未改。同一套 B，thinking 关，`max_tokens=184`。窗口 600s，墙钟 618.6s（含收尾）。数据：`/tmp/pd-029-mooncake-c32-10m`。

| 项 | 同栈 c20 × 90s | **c32 × 600s** | 旧两路 1P+1D c32 × 600s |
|---|---|---|---|
| 成功/提交 | 322/322 | **1219/1219** | 896/896 |
| 每秒完成 | 3.192 | **1.971** | 1.465 |
| 中位 / 95 分位 | 5.48s / 7.62s | **13.52s / 33.38s** | 23.48s / 25.76s |
| 90 / 99 / 最大 | — | **29.30s / 42.45s / 53.80s** | — / — / — |
| 引擎 | 四路活 | **四路活，picks +409/+415/+396** | 两路 |

端到端分布：0–5s 9 条，5–8s 127，8–10s 250，10–12s 122，12–15s 197，15–20s 196，20–30s 214，30–45s 99，45–60s 5，≥60s 0。≥10s 占 68%（833/1219）。

按完成时刻切：前 2 分钟中位 8.71s（354 条，约 3 QPS）；之后 15–18s（约 1.8 QPS）。P 的 KV 打到 82–95%，三路抢占合计 720 次。

引擎均摊（1220 条）：P 排队 **8.61s**，P 预填充 **5.84s**，NIXL **15.4ms**（失败 0，约 303MB/条），D 排队 **32ms**，D 出词 **1.01s**，MTP 93885/130324 = **72.0%**。客户端 16.01s 平均 ≈ P 侧 14.83s + 出词 1.01s。卡点在三张 P 的排队和预填充，不是 NIXL，也不是出词。

本地 GPU prefix 仍 37.9%。P 跨实例命中 24.1 万 token（约 0.96% query）。Master 20.96/24 GiB，仍在驱。`:8500` 200。其他容器未动。
