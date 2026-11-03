# HyperRAG submodule（dev3.0）

## 分支关系

- **`dev3.0` 从 `dev-2.0` 拉出**，创建时与远端 `dev-2.0` 指向同一提交（当前为 `3936c279`）。
- 后续 **ViKnow Platform 对 HyperRAG 的改造只提交到 `dev3.0`**，不直接在 `dev-2.0` 上改。
- viknow2 里 submodule 仍跟踪 `dev-2.0`，且可能 pin 在更旧的 commit；viknow-platform 使用 **`dev3.0` 分支名** 更新 submodule，与 2.0 线解耦。

## 仓库位置

```text
external/HyperRAG   # branch = dev3.0
```

```bash
git submodule update --init --remote external/HyperRAG
# 在 external/HyperRAG 内改完 push 到 origin/dev3.0，再在 viknow-platform 提交 submodule 指针
```

## 配置（简洁）

- 单一配置文件：`VIKNOW_HYPERRAG_CONFIG` 指向 `hyperrag.yml`；未设置时使用仓库内 `config/hyperrag.yml`（必须存在）。
- `${ENV}` 由 `materialize_hyperrag_config` 展开；可选 `VIKNOW_HYPERRAG_REPO_ROOT`（默认向上查找 `external/HyperRAG`）。
- `client.py` **只**走真实 `adapter.py` → submodule，无 Postgres 桩。
- 进程 lifespan 始终 `start_background_runtime()`。
- 示例：`config/hyperrag.yml.example`（复制为 `config/hyperrag.yml` 并填环境变量）。
- 接入层不重复 HyperRAG 旋钮；连接信息由 yml + 环境变量（或后续 ops 导出）提供。

## 集成方向

`access/rag` → `application/hyperrag/client.py` → `adapter.py`（5175 同源）→ `external/HyperRAG`（**dev3.0**）。

SDK 与前端只走 `/api/v1/knowledge/*`。

运行与 236 联调见 [hyperrag-runbook.md](./hyperrag-runbook.md)（`config/hyperrag.yml` + `pip install -r requirements-hyperrag.txt`）。
