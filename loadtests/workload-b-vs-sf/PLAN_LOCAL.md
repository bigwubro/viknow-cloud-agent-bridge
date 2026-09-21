# :8500 工作负载 B

和硅基 / Neolink 同一套 B：共享前缀 + 独特尾巴，目标 3 万 prompt，thinking 关，`max_tokens=184`。  
**只打** `http://127.0.0.1:8500`，不改启动参数，不打 `:5180`。

```bash
VIKNOW_OUT_DIR=out/local-b-30k-<date> ./run.sh local
```

这次实际 `COOLDOWN_SEC=15`（和云端两趟对齐）。结果见 `RESULTS_LOCAL.md`。
