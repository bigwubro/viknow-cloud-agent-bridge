# ViKnow 手动 compose（客户私有化样式）

镜像 + `deploy.env` + compose，**不依赖 Token Gateway、不依赖 Langfuse**。

## 与 `config/test` 默认的差异

镜像内 `config/test/app.yaml` 默认主聊天走 `token_gateway:viknow-agent-chat`；客户私有化应 **直连 LiteLLM**（OpenAI-compatible `private` provider）。

本目录通过 **挂载覆盖** 实现（无需改镜像）：

| 文件 | 作用 |
|---|---|
| `manual-config/app.yaml` | `share.agent.model_ref` → `private:${VIKNOW_PRIVATE_MODEL}`；`observability.langfuse.enabled: false` |
| `manual-config/tools/multimodal.yaml` | 多模态工具同样走 `private`，避免引用 `token_gateway` |

## deploy.env 要点

- `VIKNOW_PRIVATE_BASE_URL`：客户 LiteLLM 地址（例如 `http://litellm:4000/v1`）
- `VIKNOW_PRIVATE_MODEL` / `VIKNOW_PRIVATE_API_KEY`：模型名与密钥
- **不要**配置 `VIKNOW_TOKEN_GATEWAY_*`
- `VIKNOW_LANGFUSE_ENABLED=false` 且 Langfuse 密钥留空；仍需在 `app.yaml` 里 `observability.langfuse.enabled: false`（仅 env 不够）

## 启动

```bash
docker load -i viknow2-app.tar   # 客户侧
# 填写 deploy.env 密钥与 LiteLLM URL
docker compose -f docker-compose.viknow2-test.manual.yml up -d
```

236 联调侧车实例：`:5177`（与正式 `:5175` 并存时用 `REDIS_DATABASE=2` 跳过 leader lease）。
