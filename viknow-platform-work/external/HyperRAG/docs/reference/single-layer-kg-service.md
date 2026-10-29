# SingleLayerKGService

`SingleLayerKGService` 是 HyperRAG 的知识图谱运行时。它负责图谱与向量检索，供 `HyperRAGV2` 的 workspace 生命周期和查询接口使用。

调用方应通过 `HyperRAGV2` 创建和管理 workspace；不要直接依赖已删除的旧运行时接口。
