# HyperRAG 项目说明

HyperRAG 是支持文本、图像、音频和视频的多模态 RAG 知识搜索引擎。知识图谱和检索运行时由 `SingleLayerKGService` 提供。

## 开发环境

- Python 3.12+
- 使用 uv 管理依赖和虚拟环境
- 使用 src layout，包代码位于 `src/hyperrag/`

## 常用命令

```bash
uv sync
uv run pytest
uv run ruff format .
uv run ruff check .
```

## 工程约定

- 新增核心能力应复用 `SingleLayerKGService`，不保留旧运行时兼容层。
- 使用 Loguru 记录异常，并通过 `logger.exception()` 保留调用栈。
- 测试代码放在 `tests/`，执行脚本放在 `scripts/`。
- 文档保持简洁，描述当前实现而非已删除的实现。

## Jetson 与 236 源码同步（长期授权）

- Jetson 上的修改通过相关验证、提交并合入本地主线后，及时同步并合入 236 的 `/home/wlk/program/viknow2/external/HyperRAG` 主线，无需逐次询问。当前主线为 dev-2.0，不将历史 main 当作当前开发主线；不能只留在导入分支。
- 同步前检查两端工作区和提交差异，保留已有工作，使用快进或正常合并，不强推或重置覆盖。服务器远程使用 236，来源引用使用 jet/xxx 或 236/xxx，与 origin 区分。
- 明确要求隔离的未完成或未验证实验仍保留实验分支。此授权仅覆盖源码同步；向 origin 推送、部署和服务重启仍需用户明确授权。
