# ViKnow 手动 compose（客户 `jinhe` 配置包）

客户现场部署应使用 **`config/jinhe/`**（Gitea `viknow2` 仓库），不是 `config/test` 上的临时挂载补丁。

## 正确做法

1. 镜像内或挂载 **`config/jinhe/`** 整包（`app.yaml` / `deploy.env` / `hyperrag.yml` / `tools/` …）
2. 启动时指定：

```yaml
environment:
  VIKNOW_CONFIG: /opt/viknow/config/jinhe/app.yaml
env_file:
  - ./deploy.env   # 客户填写密钥与 LiteLLM URL
```

3. `config/jinhe` 已约定：
   - 主聊天 **`private:${VIKNOW_PRIVATE_MODEL}`** → 客户 LiteLLM（OpenAI-compatible `/v1`）
   - **无** `VIKNOW_TOKEN_GATEWAY_*`
   - **`observability.langfuse.enabled: false`**（仅 env 不够）
   - `tools/multimodal.yaml` 同样走 `private`
   - `hyperrag.yml`：`knowledgebase_summary_max_chars: 32000`

分支 **`agent/config-jinhe-customer`**（待合入 `main`）已按上述对齐；当前测试镜像 `84e833a` 尚未 bake `jinhe`，236 联调通过 **bind-mount 仓库内 `config/jinhe`**。

## 236 联调示例

```bash
# compose 挂载 repo 内 jinhe，端口 5177
VIKNOW_CONFIG=/opt/viknow/config/jinhe/app.yaml
volumes:
  - /path/to/viknow2/config/jinhe:/opt/viknow/config/jinhe:ro
```

运行时 `deploy.env` 填密钥与 `VIKNOW_PRIVATE_BASE_URL`；与仓库模板分离，勿把密钥提交进 Git。

## 本目录 `manual-config/` 说明

早期为验证「无 Langfuse / 无 Gateway」在 `config/test` 上的 **挂载覆盖**，现已 superseded by **`config/jinhe`**。保留仅供对照，新客户请只用 `jinhe`。
