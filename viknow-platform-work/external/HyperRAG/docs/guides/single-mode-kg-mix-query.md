# 单模式 KG Mix Query 设计说明

## 总览

这次实验保留现有的轻量单层知识图谱建图路径，并将 workspace 查询路径统一收敛为单一检索模式：`mix`。

整体设计目标是优先保证效率：

- 以 chunk 向量检索作为主召回路径；
- 以知识图谱检索作为补充，为结果提供实体、关系和支撑 chunk；
- 默认从查询热路径中移除查询时 LLM 调用；
- 仅对已经命中的实体做跨文档 synonym 扩展。

本文档用于团队内部共享，后续如果实验方案继续演进，应与代码改动一起同步更新。

## 模块一：知识图谱建图

### 设计目标

- 建图延迟要足够低，支持高频实验迭代；
- 只保留真正对检索有价值的字段；
- 避免引入高成本但查询收益不明确的 LLM 产物。

### 实体字段

当前图上保留这些实体侧字段，因为它们都直接服务于检索或 synonym 扩展：

- `entity_name`
- `entity_type`
- `entity_description`
- `file_path`
- `chunk_ids`
- `aliases`
- `normalized_name`
- `alias_keys`

这些字段已经足够支持以下能力：

- 实体向量召回；
- 跨文档 synonym 扩展；
- 将实体命中结果反查回支撑 chunk。

### 关系字段

关系侧保持现有的轻量结构，并在本轮新增一个字段：

- `source_entity`
- `target_entity`
- `relationship_description`
- `situation`
- `relation_keywords`
- `file_path`
- `chunk_ids`

`relation_keywords` 是这轮唯一明确新增的语义字段，因为它能在查询时提高关系检索的稳定性，也符合 HyperRAG 风格检索里关系侧关键词信息的实际作用。

### 为什么不增加 chunk 侧图字段

这套设计不会新增类似 `chunk.entity_names` 或 `chunk.relation_keys` 这样的字段。

原因是：

- 官方 HyperRAG 的 `mix` 路径并不依赖这些字段；
- 支撑 chunk 已经可以通过 entity 或 relation 的 `source_id` / `chunk_ids` 回收；
- 给 chunk 增加额外语义字段会放大建图负载和抽取复杂度。

### 建图阶段的取舍

整个建图流程仍然坚持效率优先：

- 不生成面向查询 rerank 的额外元数据；
- 不给关系增加更长的摘要类字段；
- 只请求较短的 `relation_keywords`，当模型未返回时再走本地 fallback 推导。

## 模块二：检索

### 单模式策略

新的查询路径只保留一种模式：`mix`。

它的固定执行流程如下：

1. 对查询做归一化，并生成本地 fallback 关键词；
2. 计算一次查询向量；
3. 并行从 pgvector 中检索 chunk、entity、relation 候选；
4. 仅对已命中的实体做跨文档 synonym 扩展；
5. 通过 entity 和 relation 的 `chunk_ids` 回收支撑 chunk；
6. 采用 round-robin 策略融合向量 chunk 和 KG 支撑 chunk；
7. 将 chunks、entities、relations 一并传入生成阶段上下文。

### 为什么这样更快

这条检索路径在工程行为上尽量贴近 HyperRAG 的 `mix`：

- 一次 embedding 结果复用给所有向量检索；
- chunk 召回严格受 top-k 限制；
- 图扩展只发生在很小的命中集合上；
- synonym 扩展改成批量执行，避免逐实体 round trip；
- 查询时不再引入 rerank 模型。

### 候选压缩策略

为了控制候选规模，查询阶段采用以下规则：

- 第一阶段的实体和关系命中会尽早截断；
- synonym 扩展只作用于顶部实体种子集合；
- 支撑 chunk 只从选中的 entity / relation 命中中回收；
- 最终 chunk 合并仍受 top-k 预算限制。

### 关系检索的定位

关系检索是补充路径，而不是第一跳主召回路径。

它主要在以下情况发挥作用：

- 查询意图本身更偏关系，而不是单纯实体；
- 直接 chunk 向量召回效果偏弱；
- 实体命中后还需要更具体的操作上下文。

`relation_keywords` 作为轻量相关性特征，与以下信号共同工作：

- 关系向量相似度；
- 关系两端实体的支撑强度；
- 与用户查询之间的文本重叠度。

### Synonym 扩展策略

跨文档 synonym 扩展被有意限制在较小范围内：

- 只有当前 workspace 查询中已经命中的实体才会触发扩展；
- 扩展依赖 `normalized_name` 和 `alias_keys`；
- 扩展采用批量查询，避免 N+1 数据库访问；
- 扩展出来的实体只作为补充，不作为主召回入口。

### 生成阶段输入契约

检索输出会同时保留三层上下文：

- chunks 作为直接证据；
- entities 用于对象级 grounding；
- relations 提供结构化语义上下文。

这符合实际使用场景：图检索更适合作为纯向量检索的补充，而不是替代。

## 当前实现状态

这一轮已经引入了：

- 独立查询引擎 `src/hyperrag/mix_query.py`；
- 在实验入口中完成单模式 `mix` 的 workspace 查询接线；
- 将关系侧 `relation_keywords` 贯穿到抽取、图持久化和向量元数据。

## 后续更新要求

后续如果实验修改了图字段设计，或者调整了检索行为，需要在同一个补丁中同步更新本文档。
