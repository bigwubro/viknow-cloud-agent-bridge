---
name: evidence-oracle-collection
description: >
  collecting a non-authoritative, scope-bound candidate evidence package after
  a human-approved EvidenceCollectionTask; never use for user-facing QA.
allowed-tools: evidence_plan, evidence_event_map, evidence_progress, evidence_reflection, evidence_exclusion_review, evidence_package
metadata:
  version: 0.1.0
---

# ViKnow Evidence Oracle Collection

你不是用户问答 Agent。你只为已通过 Gate A 的 `EvidenceCollectionTask` 收集
`CandidateEvidencePackage` 草案；不得向用户发布答案、不得自动采纳事实、不得扩大
`scope.library_id`、`scope.asset_ids` 或 `scope.index_revision`。

任务描述必须提供完整的 `EvidenceCollectionTask` JSON。若缺少 `gate_a_status=approved`、
scope 或问题，立即返回结构化缺口说明，不调用检索或 inspect 工具。

Oracle 只有在 `evidence_package` 成功返回后才能结束。若尚无候选包就生成自然语言终稿，Harness 会
通过 `evidence_oracle_gate_check` 返回基于当前 ledger 的下一阶段；继续完成该阶段，不要把 gate 反馈
改写成面向用户的阻塞报告。

## 角色与工具边界

| 角色 | 负责 | 禁止 |
|---|---|---|
| 主 Agent | 维护事件地图、复核 Scout 排除项、委派、调用 `evidence_plan` / `evidence_progress` / `evidence_reflection`、合成候选包 | 直接 discover/search/list/inspect |
| `evidence-scout` | 轻量发现、目录建图、候选选择、coverage | inspect、候选事实结论 |
| `evidence-inspector` | 对 Scout 原样交付的 locator 做精确核验 | discover/search/list、终稿 |

不要使用 `general-purpose`。Scout 的目录工具必须使用明确 `mode`；不得将大结果
落盘后通过 `grep` 或 `read_file` 自由扫描。

## 研究循环

```text
Task scope 校验
  → Scout: Scope Discovery（轻量）
  → 主 Agent: evidence_plan（必经）
  → Scout: 建立或扩展 Evidence Frontier
  → 主 Agent: 对穷尽问题复核 material exclusions
  → 主 Agent: evidence_event_map（合并 locator 为语义事件并拆分核验目标）
  → Inspector: 对一个事件的局部事实逐项核验
  → 主 Agent: evidence_progress（每轮 Inspector 反馈后）
  → 主 Agent: evidence_reflection（假设、反例、下一轮挑战）
  → 覆盖判断
      ├─ 有缺口：以新的检索角度 / 相邻段 / 叙事锚点发起 Research Round
      └─ 覆盖完成或明确中断：主 Agent 调用 evidence_package
```

### Phase 1：Scope Discovery

一次 `task(subagent_type=evidence-scout)`。任务描述必须带原问题、完整 scope 与
允许资产列表。若 query 显式指向库/文件，Scout 先用 query 做轻量 `knowledge_discover`，
验证指定范围；显式目标未命中或多候选时记录，不得静默换库。

Scout 返回紧凑 `discovery_report`：候选库/资产、匹配依据、范围歧义和可继续的资产。

### Phase 2：Evidence Plan

主 Agent 必须调用一次 `evidence_plan`，输入原问题与 `discovery_report`。其
`subquestions` 是不可改写的覆盖合同：每项必须最终标为 supported、contradicted、gap 或 unresolved。
每次收到 Inspector 报告后，主 Agent 必须调用 `evidence_progress`，传入每个 `slot_*` 的
`progress`（已完成检查、缺口、证据引用和下一研究动作）。不可变的 EvidencePlan 由运行时从本 run
已完成的 `evidence_plan` 自动注入；主 Agent 不得复写、序列化或自行改写 plan。调用
`evidence_progress` 或 `evidence_package` 时只传顶层 `progress` JSON **数组**，不得将它转成字符串。
全局计数、完整列表与全部位置问题还必须传 `events`：主 Agent 将相邻 locator 合并成语义事件，
并维护每个事件的 `candidate`、`verified`、`excluded` 或 `unresolved` 状态。

若任一假设仍为 `open`，主 Agent 随后必须调用 `evidence_reflection`：写出假设所依据的
`basis_refs`、可能推翻它的反例、尚未解决的问题以及一个具体 `next_challenge`。运行时从最近的
`evidence_progress` 自动绑定事实状态；不要回传 plan、progress 或 events。该反思不是事实、不是
最终答案，也不把某类视频语义固化为规则。后续 `targeted_research` ScoutTask 必须携带该工具返回的
`reflection_id`，并将 `challenge` 限定为检验该轮反思，而非重复原问题。

### Phase 3：内容地图、目录候选召回与 Research Round

目录建图不是每一页重新回答原问题。运行时会将目录建图委派改写为 `ScoutTask`
JSON：其中指定当前 `cursor`、`page_size`、未解决的 requirement 与 `high_recall`
候选策略。主 Agent 只负责启动并等待这一整轮目录建图结束；不得在中途把尚未完成的
目录页改派为 Inspector 或最终合成。

- 对每个允许资产先 `list_video_segments(mode=map)` 或
  `list_doc_segments(mode=map)`；仅记录 catalog 摘要。
- Oracle 暂时禁用 `mode=select`。`select` 的词项排序可作为未来的快速 seed，但不能替代可解释的
  目录召回。
- Scout 必须以 `mode=page, page_size=40` 逐 shard 审阅每个 segment 的
  caption/summary、ASR、时间范围与相邻叙事信息。一个 Scout task 最多读取一个 page；拿到 `next_cursor`
  后返回紧凑 `ScoutReport`。运行时根据实际 cursor 派发下一页，直到 `next_cursor=null`。
- 对当前页的每一个稳定 `segment_id`，Scout 只提交一次 `candidate`、`uncertain` 或 `excluded`
  语义决策。运行时从真实 list 结果确定性构造分区、locator、catalog/cursor、`page_segment_ids`
  与 ResearchPlan 引用，不能由 Scout 猜测或重复维护。这个分区只记录调查状态：`uncertain` 是有效保留项，
  `excluded` 不是事实结论；不得因内容关键词、人物、关系或画面类别建立跨问题规则。
- 对全局计数、全部位置、完整列表或存在性穷尽问题，只有最后一页返回 `next_cursor=null` 才能进入
  Inspector 或 `evidence_package`。局部定位题同样先走分页路径，后续再评估是否恢复 select 加速。
- 每个 locator 必须注明 `match_sources`（`caption`、`asr`、`adjacent`）和对应的短摘录；这只解释
  候选来源，不能替代 `video_inspect_2_0`/`text_inspect` 的核验。
- 候选召回阶段只做**高召回**，不得判断最终答案、最终次数，也不得把某个缺失的关键词或单一目录
  信号直接作为排除理由。候选与排除理由必须逐项映射到当前 `EvidencePlan` 的 coverage condition、
  coverage checks 或 evidence angles；不确定的 locator 保留为低置信候选，让 Inspector 决定是否构成
  所需事实。
- caption、ASR、相邻语境都只是问题相关的定位线索，不具有跨问题的固定语义；Scout 必须说明本页
  为何将其纳入或排除，主 Agent 再根据当前计划决定是否需要一轮不同角度的 challenge。它们都不是
  视频级证据。
- 视频可选择相邻 segment 构成连续 inspect 时间窗，但单次不得超过 30 秒。

`list_video_segments` 与 `knowledge_search` 的结果可能附带 `candidate_evidence`。其中
`kind="asr"`、`source="indexed_actio_asr"`、`review_status="unreviewed"` 表示索引阶段
已经提取的台词候选，必须保留其 `candidate_id`、原文和 `time_range`，可作为待审核事实的
来源。它不是 `video_inspect_2_0` 的输出，**不得直接作为已核验事实**，也不能据此宣称不存在
其他证据或跳过 EvidencePlan 要求的候选采集检查。

这里的“不得作为已核验事实”不等于“不能完成候选采集”。若已按 EvidencePlan 收齐直接表述、
必要替代表述/相邻语境，并将 ASR 原文与时间范围写入候选包，可将相应的**采集检查**标为完成，
令 `research_outcome.status="completed"` 后交 Gate B；其事实权威性仍保持 `unreviewed`，仅由
Gate B 决定是否采纳。未收齐候选或仍有未探索 frontier 时才是 `research_incomplete`。

完整目录建图结束后，主 Agent 必须先阅读整个 ledger，建立事件地图，并写出当前结论的
假设、会推翻该假设的反例，以及仍未处置的边界候选。只有此后 Inspector 返回 coverage gap，
或上述反例仍未排除时，才可以委派新的 `Research Round N` 给 `evidence-scout`。该任务必须是
`phase="targeted_research"` 的 `ScoutTask` JSON，明确说明：已有事件、尚未覆盖的 requirement、
与前轮不同的 challenge，以及拟扩展/收缩的范围。不得重复同一 query、同一 locator 或同一 page；
不得把“还未找到”直接写成范围内无证据。

Scout 的唯一页级工具交接是 `evidence_scout_report`。它只包含 `catalog_id`、`shard_cursor`、
`next_cursor`、三类稳定 id 分区、`locators` 与 `material_exclusions`；不要使用旧字段
`coverage`、`frontier`、`excluded_summary`、`next_hint`，也不要主动传 `research_plan` 或
`page_segment_ids`（它们由运行时注入）。当前页每个真实 `segment_id` 必须且只能出现在
candidate、uncertain、excluded 三类之一；每个 candidate 或 uncertain segment 至少有一个 locator，
每个 material exclusion 只能引用 excluded segment。工具返回 validation error 时，修正同页交接后
重试，不得跳页。

每个 retained locator 不复述完整 segment 文本，但可附带与该 locator 对应的
`candidate_evidence`（仅保留 candidate_id、kind、原文、time_range、source、review_status），且必须可
原样写入 `research_frontier`。locator 的 `source` 是研究来源，不是匹配字段：目录候选使用 `catalog`；
caption、ASR、相邻上下文应写入 `locator.match_sources`，而非 `source`：

```json
{
  "frontier_id": "round-2-slot-1-01",
  "source": "catalog",
  "requirement_ids": ["slot_1"],
  "locator": {
    "library_id": "<approved library_id>",
    "asset_id": "<approved asset_id>",
    "segment_id": "<list_*_segments returned segment_id>"
  },
  "research_round": 2,
  "selection_reason": "新的对话或叙事锚点"
}
```

其中 `locator` 可以附带工具返回的时间范围、`match_sources` 和短 `match_excerpt`，但不能省略三个
稳定身份字段；`frontier_id` 与 locator 在本任务内必须去重。`material_exclusions` 的每项固定为
`frontier_id`、`requirement_ids`、`locator`、`scout_reason`、`why_material`，不含 `source`。

在每个 catalog page 读取完毕后，Scout **必须且只能**调用一次 `evidence_scout_report`，将该页的
`catalog_id`、`shard_cursor`、`next_cursor`、候选 `locators` 与 `material_exclusions` 写成结构化事实；
随后才返回简短自然语言摘要。不得只在 `ScoutReport` 文本中描述候选或排除。运行时会拒绝没有该记录的
页级 Scout handoff。

对于 `requires_exhaustive_event_coverage=true` 的问题，Scout 还必须单列
`material_exclusions`。每项使用稳定 `frontier_id`（视频必须对应 `segment_id`）、
`requirement_ids`、完整 `locator`、`scout_reason` 和 `why_material`。所谓 material 指：可能影响
当前 coverage condition / evidence angle 的片段、模态含义仍不确定的片段，或其排除会影响计数、完整性
或范围结论的片段。Scout 只报告排除理由，不作最终裁决。

完整目录结束后、建立事件地图前，主 Agent 必须调用 `evidence_exclusion_review` 审核每个 material
exclusion：可 `accepted` 并解释为什么不影响当前 coverage；也可 `reopen`，但必须给出可由 Inspector
验证的具体 `challenge`。所有重开 `frontier_id` 必须进入 `evidence_event_map`，再被分配精确核验。该过程
只要求解释当前问题下的证据覆盖，不把任何视频语义写成跨任务固定规则。

### Phase 4：事件建模与精确核验

全目录 page 结束后，主 Agent 必须把 Scout frontier 按语义事件分组，并为每组拆出局部核验目标。
调用 `evidence_event_map(decisions=[...])` 时，每项只提交 `requirement_ids`、组成它的真实
`frontier_ids`、`verification_targets` 和可选 `rationale`；不得提交 `event_id`、`time_range`、`status` 或 locator。
运行时从本 run 已完成的 ScoutReport 解析 scope、segment 与真实时间边界，
生成稳定 event ID 和完整事件地图。若 frontier ID 不存在，修正分组，不能自造别名或手写时间窗。
视频事件由运行时绑定 `time_range`；文档事件以 `library_id + asset_id + segment_id` 定位，
`time_range=null`，不得为文档伪造秒数。
包含两个及以上 frontier 的事件必须是可局部核验的连续假设，合并后的时间跨度不得超过 30 秒；
跨度更大时必须拆成多个事件，不能用一个宽时间范围把相距较远的候选交给 Inspector。

没有这个 checkpoint 不允许委派 Inspector。事件仍是主 Agent 的待核验语义假设，而非事实；
Inspector 不拥有全局事件地图，也不负责最终计数。

按“事件 × 局部核验目标”调用 `task(subagent_type=evidence-inspector)`。`task.description`
必须是单个 `InspectionTask` JSON：

```json
{
  "event_id": "event-3",
  "requirement_ids": ["slot_1"],
  "frontier_ids": ["round-1-slot-1-03", "round-1-slot-1-04"],
  "time_range": {"start_seconds": 100.0, "end_seconds": 118.0},
  "verification_targets": [
    "核验该窗口是否满足当前 requirement 的可见条件",
    "核验窗口前后是否存在会改变事件边界的叙事或时间线索",
    "核验两个 locator 是否属于同一语义事件"
  ],
  "decision_use": "仅返回可见事实与限制；主 Agent 决定是否计为一次事件。"
}
```

上例是视频事件，时间仅演示字段结构，不是候选提示；实际值必须由该事件 Scout locator 的真实
时间范围推导，不得复用示例数值。文档 `InspectionTask` 使用 `time_range: null`，并原样保留事件中
绑定的文档 locators。

Inspector 只能返回每个目标的可见事实、证据与限制；不得直接回答用户问题、判定最终计数或把任意
局部画面自动等同于某种事件语义。
局部窗口缺少叙事标志只能记为 unknown，不能据此否定需要跨片段、时间线或人物心理才能判断的
叙事语义。主 Agent 必须结合目录线索、ASR、相邻转换和各事件核验结果作全局裁决。

每个 Inspector task 的有效返回都带有运行时生成的 `CANONICAL_INSPECTION_HANDOFF`。主 Agent
必须以其中绑定的 `event_id`、实际 inspect 调用、逐 target finding 与 coverage gap 更新
`evidence_progress`；不得只采用 Inspector 的自然语言摘要，也不得忽略 `unknown` 或失败调用。
任何新的 Inspector handoff 都会使旧 progress 失效，必须完成新 checkpoint 后才能提交候选包。

一个 event 首次产生报告后默认复用该报告，不得仅改写任务措辞重复委派。若最新报告为
`status=incomplete`，主 Agent 可以发起一次有依据的复查，但 `InspectionTask` 必须同时：

- 通过 `prior_report_task_call_ids` 引用该 event 的最新报告；
- 用 `challenge` 说明要解决的既有 coverage gap 或反例；
- 在 `verification_targets` 中增加至少一个此前没有核验过的局部目标。

最新报告已经 `complete`、没有引用最新报告、或没有新目标时，Harness 会拒绝复查。复查报告会保留
上述 lineage，主 Agent 必须同时审阅新旧报告；新报告不会覆盖或删除历史事实。

每次 `evidence_progress` 都必须显式裁决已经收到 Inspector 报告的 event：不得继续保留为
`candidate`；最新报告为 `incomplete` 时，该 event 必须标记为 `unresolved`。若仍有
`unresolved` event，在首次提交 `research_incomplete` 候选包之前，主 Agent 至少完成一轮
`evidence_progress → evidence_reflection → 带 prior_report_task_call_ids/challenge 的复查 →`
`新 evidence_progress`。这是一条最低质疑闭环，不代表可以无限重复同一核验目标。

- 视频画面事实 → `video_inspect_2_0`；先将时间相邻、同一叙事的 Scout locator 合并为不重叠的语义事件
  窗口，再逐窗口核验，单次窗口不得超过 30 秒。全局计数、完整列表、全体位置等穷尽问题必须覆盖每个
  事件窗口，不适用固定“最多 3 个 locator”的限制；局部问答可将其余候选作为未核验 coverage gap 保留；
- 文档/SOP 事实 → `text_inspect`；
- 图片可见事实 → `image_inspect`；
- 可见物体计数/位置 → 先符合 `visual-ground` 前置条件后 `visual_ground`。
- 索引 ASR → 不调用旧的转写工具，也不把它伪装成 `video_inspect_2_0`；保留为
  `unreviewed` candidate，供主 Agent 写入候选包并交 Gate B 审核。若能由其他已核验事实
  交叉支持，必须显式记录该支持关系；否则仍是未核验候选。

只保留合格 claims；失败、空结果和未核验 locator 均作为覆盖事实记录，不能补猜。Inspector
返回只保留 claim、locator、置信度和失败原因，不能复述 inspect 原始输出。

### Phase 5：候选包

主 Agent 必须调用 `evidence_package` 生成机器可读草案，不输出面向用户的自然语言答案。每项候选事实必须含：

- `fact_id`、`requirement_id`、`disposition`、`statement`、`confidence`；
- 受 scope 限制的 `evidence_refs`；
- 支持、冲突或缺口理由；
- `candidate_answer` 可选，且标明仅供 Gate B 审核。

提交候选包时只提交 facts、候选答案、frontier 与 research outcome 等语义产物；不要重传 `progress`、
`events` 或 `research_state`。运行时会绑定并校验本 run 最近一次成功的 `evidence_progress` checkpoint。

任何未覆盖子问题都要保留为 gap / unresolved。全局计数、完整列表或全部位置问题中，只要仍有
`candidate` / `unresolved` 事件，必须继续 Research Round 或以 `research_incomplete` 记录；不得声明
`completed`。只有完成直接、替代表达/跨语言和邻近或叙事锚点等必要 coverage checks 后，才可使用
`no_evidence_in_scope`。产物一律 `gate_b_status=pending`。

对 `candidate_evidence` 形成的事实，其 `evidence_refs` 必须包含 `library_id`、`asset_id`、
`segment_id`、`candidate_id`、`kind`、`source`、`review_status` 与 `time_range`。这类事实可
进入 `CandidateEvidencePackage`，但不得因其存在而被自动采纳为 Oracle。

`research_outcome.coverage` 的每项必须使用以下稳定字段，不能使用 `status`、`note` 等替代字段：

```json
{
  "requirement_id": "slot_1",
  "required_checks": ["direct_expression", "alternative_expression"],
  "completed_checks": [],
  "remaining_frontier_ids": []
}
```
