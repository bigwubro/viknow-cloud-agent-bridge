# 分块语义完整性优化总结

## 背景

本次工作以三份固定 `Asset + Segment` 文档快照的端到端语义完整性审查为目标。主指标是
`chunk_macro_score`，`chunk_token_weighted_score` 用于监控明显回退。正式索引链路统一为
`SingleLayerKGService`；历史 LightRAG 兼容路径已移除，不参与评测或生产运行。

评测要求是：候选和基线使用相同快照、配置、tokenizer 与 judge 策略，真实写入
`SingleLayerKGService` 和 `KGVectorStore`，再由标准 review loader 读取。任一 chunk 未获
语义评分时，该轮结果无效，不能从分母静默排除。

## 优化方案设计

1. **固定输入与可复现实验**
   - 冻结三份 `Asset + Segment` 快照、chunker 源码、脱敏 resolved config、runner 源码和
     judge 参数。
   - 基线与候选隔离 workspace；所有 chunk 都经原生向量存储和索引审查链路评测。
   - judge 使用 `hyperrag`、`Qwen/Qwen3.6-35B-A3B-FP8`、`max_retries=3`，不启用评分失败
     回退。

2. **开源策略调研与筛选**
   - 比较了 LangChain `RecursiveCharacterTextSplitter`（O0）、LlamaIndex
     `SentenceSplitter` 和 Markdown Header + Recursive 等方案。
   - O0 的固定快照离线实验在完整来源映射约束下最优，目标预算为 448；语义 splitter 方案若
     不能映射回连续、完整的 source segment，则标记为无效而非展示分数。

3. **生产融合**
   - `HyperChunker` 对连续文本使用 448 token 的目标预算，调用方的运行时硬上限仍为 512。
   - 维持既有标题绑定、章节边界、modal flush/replay、caption、表格表头续接、metadata 和
     provenance 逻辑。
   - 纯 `image` segment 保持单一原子 chunk，允许超过预算；所有 `table` segment（即使其
     metadata 为 `chunk_type=image`）均按表格感知规则在预算内切分，避免大表格过长。

## 实验结果

### O0 离线筛选

| 方案 | 参数 | chunk_macro_score | chunk_token_weighted_score | 结论 |
|---|---:|---:|---:|---|
| O0 LangChain Recursive | 448 | 0.5703 | 0.5787 | 离线最佳候选 |
| O0 LangChain Recursive | 480 | 0.5169 | 0.5186 | 低于 448 |
| O0 LangChain Recursive | 512 | 0.5224 | 0.5255 | 低于 448 |
| O4 Markdown Header + Recursive | 480 | 0.5571 | 未作为主指标 | 次优 |

上述仅用于筛选候选；生产是否保留仅依据下述冻结原生 A/B。

### 生产原生 A/B

| 运行 | 基线 macro | O0 macro | 基线 weighted | O0 weighted | 完整审查 chunk |
|---|---:|---:|---:|---:|---:|
| 首轮 | 0.6144 | 0.6189 | 0.7081 | 0.7156 | 84 / 84 |
| 复现 | 0.6172 | 0.6174 | 0.7130 | 0.7154 | 84 / 84 |

逐字比较候选与基线输出表明，PDF 和 Markdown 的 chunk 内容并未变化，其评分波动不能作为
O0 收益。DOCX 是实际受影响的文档：最大连续文本 chunk 从 499 token 降至 437 token，且
两轮 macro 均可复现提升。

| 运行 | 基线 DOCX macro | O0 DOCX macro | 变化 |
|---|---:|---:|---:|
| 首轮 | 0.6648 | 0.7030 | +0.0381 |
| 复现 | 0.6796 | 0.7093 | +0.0296 |

因此，本次保留 O0 融合的依据是 DOCX 上的可复现改进；两轮加权分均上升，未观察到明显
`chunk_token_weighted_score` 回退。

## 后续建议

1. 每次候选评测前继续归档源码副本、快照、脱敏配置和审查策略；源码 hash 不能替代源码副本。
2. 对未改变 chunk 输出的文档，不应将单轮 LLM 评分波动解释为策略收益；应先比较输出哈希。
3. 扩充存在 448--512 token 连续文本的真实文档集，复验 O0 在更多文档类型上的收益。
4. 表格质量的根因仍取决于解析器能否提供表格正文；分块只能保证已有表格文本不因超预算丢失，
   不能补全解析阶段未提取的单元格内容。
