# Qwen3.6 DCGM (`dcgm-qwen36-gpu0-3`)

Live on 236: `/home/cursor-agent/services/qwen36-dcgm-gpu0-3/`

## Symptom: Grafana `DCGM_FI_PROF_*` empty

1. Confirm sidecar Prometheus: `http://127.0.0.1:9108`
2. Restart exporter: `bash run.sh`
3. Check: `curl -s http://127.0.0.1:9401/metrics | grep DCGM_FI_PROF_PIPE_TENSOR_ACTIVE | head`

`compute-counters.csv` must **not** include unsupported fields (e.g. `DCGM_FI_DEV_XID_ERRORS` on some GPUs); a bad field can prevent profiling counters from publishing.

## Grafana hardware row

Dashboard uid `b281712d-8bff-41ef-9f3f-71ad43c05e9b` — patch with:

`python3 grafana/fix_qwen36_gpu_hw.py /path/to/grafana.db`

Panel 60 = `DCGM_FI_DEV_GPU_UTIL` (time-busy %), panel 61 = Tensor Active (0–1).
