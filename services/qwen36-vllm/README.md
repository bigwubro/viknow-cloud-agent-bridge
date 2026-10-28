# Qwen3.6 vLLM serve (44688-aligned)

Target flags (see `run.sh`):

- `--api-server-count 4`
- `--data-parallel-size 4 --tensor-parallel-size 1`
- `--renderer-num-workers 4` with `--mm-processor-cache-type shm`

Image `vllm-viknow:0.26.0-lmcache0.5.4-mmfix-r44688` is the current
`mmfix` image plus `patches/apply_renderer_mm_cache.py` (lift the
renderer+cache guard, RLock SHM sender cache).

Not included: unmerged upstream #44786 (cache-hit executor bypass /
single-flight) and #44787 (Qwen3-VL compact still-image patches).
Those do not apply cleanly to v0.26.0.
