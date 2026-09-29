# viknow2-wzh-5180-yace (direct 8500)

5180 压测实例直连本机 `127.0.0.1:8500`。vLLM 只认 `--served-model-name Qwen/Qwen3.6-35B-A3B-FP8`。

原先 `token_gateway:viknow-agent-chat` 会先把逻辑名 POST 到 8500，vLLM 回 `404 The model 'viknow-agent-chat' does not exist`，而且 gateway 把非 2xx 当最终结果，不会回落到 served name。

本目录补丁：

- `patches/model_gateway.py`：gateway 与 raw model 同 origin 时跳过第一跳，只发 served name
- `patches/multimodal.yaml`：caption / review / video_discovery 改为 `private:${VIKNOW_PRIVATE_MODEL}`

236 上用 `/home/cursor-agent/services/viknow2-wzh-5180-yace/run.sh` 重启容器（不要动 `viknow2-test`）。
