# Knowledge QA Skill v2 优化设计

## 文档状态

- 状态：首批保守重组已实施；完整 B 版本与真实 A/B 验证待完成
- 适用对象：`knowledge-qa-v2`
- 当前模型基线：`Qwen3.6-35B-A3B`
- 候选模型：现有 Qwen3.8 网关路由；真实 checkpoint 待确认，不将路由别名视为模型身份
- 对照模型：闭源前沿模型
- 设计性质：Skill 组织与加载方式优化，不直接改变 QA 证据语义

本文依据 `model-and-skill-optimization-guidance.md`，记录第一版可实施、可归因、可回退的
QA Skill v2 优化方案。本文不是运行时 Skill，不应由 QA Agent 加载。

本文定义近期执行范围；指导文档中的完整模型矩阵和条件注入属于后续研究方向，不是首轮前置条件。
其中论文数字和模型架构描述尚需核对原始来源，不作为本轮改造的必要前提或效果保证。

## 一、问题定义

当前 `knowledge-qa-v2/SKILL.md` 已达到 420 行，同时承担：

- 领域证据不变量；
- 问题路由与调查流程；
- 工具参数和能力说明；
- Answer Plan 状态协议；
- 多模态冲突处理；
- 全资产覆盖；
- 最终答案格式。

这些内容存在重复定义、绝对规则与分散例外、Skill 与 Runtime 职责重叠等问题。普通事实问答
也需要加载接近完整的复杂协议，增加了模型理解成本。当前有限体验还显示，直接把现有 Skill
交给现有 Qwen3.8 路由并没有稳定优于本地 Qwen3.6 的结果。历史运行混有网关、解析和超时故障，
不能据此归因于模型理解能力，也不能据路由名称认定底层 checkpoint。

本设计首先回答一个可独立验证的问题：

> 在工具、Runtime、证据语义和终止语义不变的前提下，将当前 Skill 重组为短核心协议和
> 一层场景协议，能否改善轨迹质量、上下文效率和跨模型稳定性？

## 二、设计目标

第一版优化需要：

1. 保持既有证据、来源范围、覆盖和引用不变量；
2. 将 root Skill 收敛为可扫描、允许根据新证据回访和调整的决策协议；
3. 每条规则只保留一个权威定义位置；
4. 将专项流程收敛为一层、按任务路径组织的 references；
5. 减少普通 QA 加载的无关内容；
6. 为 Qwen3.6、Qwen3.8 和闭源模型提供相同的领域语义基线；
7. 让每项收益或回归能够归因到 Skill 组织，而不是混入 Runtime 和多 Agent 变化。

第一版不以达到固定行数为成功标准。约 180～220 行只是实验起点，正确性和轨迹表现优先。

## 三、备选方案

### 保守备选：只精简当前主文件

将 420 行压缩到约 250 行，保留现有 reference 结构。

- 优点：改动小，迁移风险低；
- 缺点：reference 按概念分散、Runtime 职责混入 Skill 等问题仍然存在；
- 定位：可作为保守中间对照，不作为推荐的最终结构。

### 推荐方案 B：短核心协议与一层场景协议

将 root Skill 收敛到约 180～220 行，把专项规则按任务路径重组为少量 references，不修改
Runtime 行为。

- 优点：能独立验证 Skill 结构变化，符合渐进式披露的实证经验；
- 缺点：关键 reference 仍可能被模型漏读；
- 定位：第一轮推荐方案。

### 方案 C：方案 B 加 Runtime 条件提示

在 B 的基础上，由 Runtime 根据确定性状态提示或注入对应 reference。

- 优点：降低 Qwen 对自主 reference 路由的依赖；
- 缺点：同时改变 Skill 与 Runtime，直接实施时难以归因；
- 定位：只有 B 的轨迹证明存在 reference 漏读或错读后才进入实验。

采用顺序：

```text
A0：留存原始版本；兼容清理单独验证后冻结为共同基线 A
  → B：先验证语义等价的 Skill 重组
  → 根据 B 的加载与采用数据决定是否实验 C
```

## 四、语义不变量

第一轮优化不得改变：

- 搜索结果和分段目录只产生候选，不是最终答案证据；
- 最终知识库事实必须来自合法证据；
- 用户指定的来源属于事实范围，不得因检索结果而静默改写；
- 候选必须使用与事实类型匹配的 inspect 工具核验；
- 失败结果、`confidence <= 0` 和未通过 quote verification 的文本 claim 不可用；
- 可见对象数量和位置必须使用 `visual_ground`；
- 单帧结果不能证明整段视频累计总数；
- full-asset 问题必须完成要求的目录覆盖和候选核验；
- 历史 claim 只能复述相同事实范围，新状态需要重新核验；
- 引用必须使用合格 claim 返回的原始 token；
- 证据不足、歧义和冲突不能用通识补齐；
- 最终答案只回答用户明确要求的内容。

第一轮明确不做：

- 不启用多 Agent；
- 不改变 retriever/inspector 委派策略；
- 不修改 Answer Plan 数据模型；
- 不修改工具 schema 和工具实现；
- 不改变 answer review 阈值；
- 不新增兼容 alias、静默 fallback 或补丁字段；
- 不为不同模型维护两套领域语义不同的 Skill。

本节所列规则是待与当前生效实现逐项核对的既有约束，不因写入本设计就获得新的强制性。
如规则本身不合理，应单独提出行为变更，不以“精简”名义加强或放宽它。

### Agent 与 Harness 的职责

- Agent 判断题意、目标拆分、候选相关性、调查范围、反证需求与是否修改计划；新结果允许推翻旧解释。
- Harness 保存状态，校验权限、参数、来源关联和终态等可确定约束；工具成功不等于目标语义完成。
- 门禁反馈说明缺少的条件、已有可用事实和可选恢复方向，不强制重新 discover 或执行某个唯一序列。
- 不能因为代码会拒绝错误调用，就移除模型理解该边界所需的最短说明；拒绝后仍应有可执行的恢复信息。

## 五、合法事实来源

root Skill 应使用一个封闭白名单替代“绝对规则 + 分散例外”：

1. 当前 run 的 eligible inspect claim；
2. 相同事实范围内可复用的 historical verified claim；
3. 纯上传图片问题中的 `answer_from_image`；
4. 明确标注来源的 ViKnow 专家模型概念或实体关系事实；
5. 使用上述合格数值输入推导出的计算结果。

冲突处理沿用现有 Skill 的同事实、同范围规则，不新增“当前 claim 必定优于历史 claim”的总排序。
保留已有的当前 inspect 对专家模型的优先规则；其他冲突需结合事实范围、时效和适用模态判断，
不能只凭生成先后认定正确。不得借整理白名单扩大专家模型可回答的事实范围。

`answer_from_image` 和计算属于独立路径，不作为普通知识库来源冲突的回退。

## 六、目标文件结构

建议将 8 个现有 references 收敛为 5 个按任务路径组织的运行时协议：

```text
knowledge-qa-v2/
├── SKILL.md
├── references/
│   ├── evidence-workflow.md
│   ├── full-asset-coverage.md
│   ├── multimodal-verification.md
│   ├── computation.md
│   └── answer-generation.md
└── maintenance/
    ├── invariants.md
    ├── eval-scenarios.md
    ├── optimization-policy.md
    ├── optimization-log.md
    ├── model-and-skill-optimization-guidance.md
    └── qa-skill-v2-optimization-design.md
```

| 新 reference | 主要合并来源 | 加载条件 |
|---|---|---|
| `evidence-workflow.md` | inspect eligibility、candidate audit | 多候选、候选歧义、首次 inspect 失败 |
| `full-asset-coverage.md` | video audit、catalog coverage、collection | 全部、完整、累计、整段或整份范围 |
| `multimodal-verification.md` | modality policy、claim reconciliation | 跨模态问题或 eligible claims 冲突 |
| `computation.md` | sandbox computation rules | 必须进行精确计算 |
| `answer-generation.md` | 专项 generation 模板 | 当前任务需要复杂输出组织且主文件说明不足时；不要求每次回答前读取 |

`visual-ground` 保持为独立 Skill，不复制其工具专用请求格式。

五个 reference 是候选划分，不是必须凑齐的文件数。通用引用、单位和限制说明留在主文件；
若专项内容不足以单列，可不创建对应文件。普通 QA 以不加载或只加载一个专项 reference 为观察目标，
不是读取次数上限。降低实际上下文成本比减少文件数更重要。

## 七、root Skill 设计

### 推荐章节

```text
Scope
  → 使用范围与非知识问题退出

Evidence Contract
  → 合法事实来源、优先级、不可补猜

Route The Question
  → 上传图片、普通 QA、全量问题、计算路径

Establish Source Scope
  → 显式来源、discover、自选来源、来源歧义

Maintain The Answer Plan
  → slot、evidence scope、query refine、review

Retrieve Candidates
  → focused retrieval、候选三态、扩展条件

Verify Evidence
  → 按事实模态 inspect、eligible claim

Coverage
  → local/full-asset 分流

Finalize
  → slot closure、限制、答案与引用

Load References
  → 一层、可观察的加载条件
```

### 问题路径

root Skill 只保留四条主路径：

```text
A. 与知识库无关
   → 不调用知识工具，直接回答

B. 纯上传图片内容
   → uploaded_image_search
   → answer_from_image / mismatch / cannot_process_image

C. 普通知识库事实（同样维护初始计划与最终自查）
   → source grounding
   → focused retrieval
   → inspect
   → answer

D. 多目标或需要全量覆盖的问题（比较不自动意味着全资产遍历）
   → source grounding
   → answer slots
   → coverage plan
   → catalog/retrieval
   → inspect retained candidates
   → reconcile/deduplicate
   → answer
```

### Answer Plan 边界

root Skill 只说明：

- Runtime 已建立初始 `answer` slot；
- 只有独立新任务才调用 `start`；
- 精确直接问题可以跳过 `query_refine`；
- 多事实、歧义、比较和全量问题才拆分 slot；
- 每个 slot 必须保留 requested source 和 required evidence scope；
- plan 是调查状态，不是证据；
- 最终前需要 review 原问题和未解决 gap。

Agent 根据新证据主动更新解释、目标和剩余缺口；最终自查既核对原问题的正向要求，也检查遗漏、
冲突和结论外推。反向校验不等于所有简单问题都必须遍历全篇或证明不存在更多信息。

字段结构、revision、完整 payload 和校验错误修复方式优先复用已有 tool schema 与 Runtime 反馈。
首轮不新增工具或修改状态协议；尚未被工具描述和反馈覆盖的必要说明不可直接删除。

### 检索与核验边界

主文件只保留：

```text
确认来源（必要时 discover）→ search/catalog → candidate decision → inspect → eligible claim
```

候选继续使用三个状态：

- `keep_for_inspect`；
- `reject_with_reason`；
- `need_more_locator`。

普通检索策略保留为：

- focused query 初始优先 `top_k=5`；
- 结果无关或重复时调整 query，而不是只扩大窗口；
- 现有候选不能覆盖 slot 时才扩大窗口；
- 同一 library 下，已成功的相同 normalized query 不重复请求相同或更小窗口；
- 未解决目标需要时可增大 top_k，同一查询也可用于另一个已授权 library；
- 先 inspect 已保留候选，再继续扩大检索。

复杂候选选择进入 `evidence-workflow.md`。

### Coverage 边界

root Skill 只做分流：

- `local`：特定事实、位置、步骤或局部片段；
- `full_asset`：全部、完整列表、总次数、整段视频、整份文档、从未发生等穷尽范围。

这些是语义说明，不是字符匹配规则；由 Agent 结合题意和证据判断。局部范围内的比较或计数
不自动升级为全资产遍历；query_refine 的建议也不直接成为不可修改的硬条件。

map/page/cursor/catalog review、uncertain candidate 和 collection quantity 的细节进入
`full-asset-coverage.md`。

### 最终回答边界

root Skill 只保留：

- 每个回答 slot 必须是 verified 或带具体原因的 uncovered；
- 部分证据不能表达为总量、不存在或完整覆盖；
- 保留来源单位、币种和尺度；
- 不暴露内部 ID、路径和工具过程；
- 只生成一份用户答案；
- 通用回答无需为完成流程而额外读取 reference；复杂组织需求才读取 `answer-generation.md`。

## 八、内容迁移与清理

### 从主文件移除已有 schema 或 Runtime feedback 覆盖的细节

- `answer_plan` payload 的具体字段修复方式；
- revision/version 的维护细节；
- locator 的完整 JSON 字段要求；
- `fps=auto`、`visual_budget=balanced` 和 crop 默认值；
- execute 的文件目录和 shell 示例；
- 工具成功/失败 envelope；
- citation token 的格式校验；
- Runtime 已能拒绝的重复搜索规则细节。

迁移前建立逐条清单：原规则、语义目的、现有权威实现、目标位置、反馈是否充分及验证方式。
代码校验、参数说明与恢复引导不是同一件事，需分别检查。若必须新增 schema 或 Runtime 能力
才能删掉说明，该项移出本次结构实验。execute 的操作示例按下节移到计算 reference，不改工具。

### 移到 references

- full-asset catalog 分页流程；
- grouped collection item 和 quantity；
- 视频候选预算和扩展策略；
- inspect 失败后的完整审计步骤；
- 同事实、同 scope 的冲突矩阵；
- factual、comparison、workflow 等输出模板；
- Python 标准库、sandbox 路径和计算示例。

### 单一定义

以下规则只能保留一个权威定义，其他位置使用简短引用：

- search/catalog 不是证据；
- claim eligibility；
- visible count/location 必须 `visual_ground`；
- 索引 ASR/OCR 摘要与经核验的语音/文字证据按现有契约区分，不笼统禁止 ASR/OCR 事实；
- 单帧不是整段累计；
- 不暴露内部 metadata；
- 最终答案只写一次；
- 证据不足不能补猜。

### 陈旧合同清理

- 删除或替换 reference 中的 `video_counter`；
- 按目标 Profile 实际加载的工具名称修复陈旧引用，不借此切换工具实现或修改其他环境；
- broad answer 只有在 coverage 未完成时才声明可能不完整；
- 不再默认要求回答末尾邀请用户缩小范围；
- 多环境 segment 工具差异作为独立配置批次处理，不混入纯结构实验。

## 九、模型适配

### Qwen3.6-35B-A3B

第一轮生产候选采用：

- 约 180～220 行 root；
- 显式编号步骤；
- 合法证据来源封闭白名单；
- 明确的 local/full-asset 分支；
- 可观察的 reference 触发条件；
- 工具失败后的少量结构化恢复方向；
- 首轮不新增示例；已有必要示例只做语义等价迁移，不带 case 答案、专有对象或时间定位。

### Qwen3.8 候选路由

第一轮使用与 Qwen3.6 完全相同的 B 版本，不维护专用领域 Skill。重点观测：

- reference 漏读或无关读取；
- Answer Plan 跳过率；
- candidate 被直接用于回答的比例；
- 空工具调用和字段错误；
- 长轨迹中的 source scope 遗忘；
- full-asset 提前结束；
- 较短 root 是否改善上述指标。

如果主要问题是 reference 漏读，优先实验 Runtime 条件提示，不向 root Skill 重新追加所有规则。

### 闭源前沿模型

B 作为共同语义基线，后续可另设原则型 D 作为对照，不阻塞首轮：

- root 约 100～160 行；
- 减少机械步骤展开；
- 提高 reference 自主路由和动态规划空间；
- 允许基于明确 gap 的补充调查；
- 保持相同的证据、coverage、citation 和预算门禁。

## 十、实施批次

### Batch 0：依赖对齐、原始留存与共同基线冻结

- 保存原始 Skill、references 与依赖的内容哈希为 A0；
- 固定模型、parser、serving 和 Runtime 配置；
- 修复 `video_counter` 等陈旧名称；
- 审计多环境 sidecar 差异；
- 不改变 QA 策略。

清理单独验证后再冻结共同基线 A；A/B 共用清理成果，不将兼容修复收益归入精简。
主仓库与子模块必须组成可复现版本；远端缺失提交时暂停真实比较，不自行替换相近版本。
未提交的 query_refine 两阶段规划须明确采用或排除，并在两侧保持一致，保留用户原改动。
固定模型路由、预算、answer_review、工具清单与索引版本；无法固定的条件记录为混杂因素。
不为本实验顺带更改本地密钥、部署连接或全局服务配置。

### Batch 1：语义等价重组

- 根据迁移清单建立少量目标 references；
- 按单一权威位置迁移当前规则；
- root 收敛到约 180～220 行；
- 更新结构测试；
- 对照 maintenance invariants 检查语义完整性；
- 不修改 Runtime。

该批次产出 B 版本。

### Batch 2：先单模型配对，再扩展模型

```text
Qwen3.6-35B-A3B    × A / B
Qwen3.8 已确认路由 × A / B（后续扩展）
```

先用现有已校验的 16 题做 Qwen3.6 A/B 配对，沿用已确认的 8 并发；两侧不得同时竞争同一模型容量。
每个版本至少运行两轮；结论方向不一致时增加一轮或标记证据不足，不以最好一轮作结论。
真实运行前检查模型与工具连通性、审核输出和数据资产；外部失败单独归类，同时保留全量运行统计。
完成逐题答案、证据和过程对比后，再决定扩大至 50 题或执行 Qwen3.8（2 并发）的配对实验。
现有文档题集不能覆盖上传图片、视频和连续追问，未覆盖项保持待验证，不能宣称全模态验收。
闭源模型、C/D 变体和完整矩阵不作为本轮实施前置条件。

重点回答：

1. B 是否降低输入 token、无关 reference 和无效工具调用；
2. B 是否保持或提高关键正确性；
3. Qwen3.8 的主要失败属于加载、采用、工具调用、Runtime 还是最终综合。

### Batch 3：Runtime 条件加载实验

只有 Batch 2 证明 reference 漏读或错读实际影响执行时，才另行批准并实施 C：

- `coverage_requirement=full_asset` 时提示全量覆盖协议；
- inspect 前提示证据核验协议；
- 同 scope eligible claims 冲突时提示多模态核验协议；
- execute 前提示计算协议。

条件提示只暴露当前需要的程序知识，不替模型作语义判断，也不把 DeepAgents 恢复为固定 DAG。
采用时优先在当前工具反馈或最新状态消息中提示，并观测重复注入成本；不默认持续追加 system message。

### Batch 4：模型策略与多 Agent

最后再评估：

- 闭源原则型 D；
- Qwen3.6/3.8 的 model policy；
- 是否启用 QA 多 Agent；
- retriever/inspector 是否允许基于明确 gap 的第二轮。

该批次不与第一轮 Skill 精简混合。

## 十一、评测设计

### 任务覆盖

- 单文档直接事实；
- 指定来源；
- source mismatch 与重新发现；
- 跨文档比较；
- 上传图片纯看图与知识库联动；
- 视频局部动作；
- 单帧可见物体计数或位置；
- 整段视频或整份文档全量枚举；
- 多模态冲突；
- 数值计算；
- 无证据、部分证据和真实冲突；
- 历史 claim 复用与新状态重新 inspect；
- 连续追问与独立新任务切换。

### 过程指标

| 层次 | 指标 |
|---|---|
| 激活 | Skill 激活率、错误激活率 |
| 加载 | reference 读取率、无关读取率、缺失读取率，以及实际注入/读取的内容量 |
| 采用 | 核心规则 uptake、首次合法工具调用率 |
| 规划 | answer slot 覆盖率、来源范围保留率 |
| 检索 | 重复搜索率、有效候选率、无进展轮数 |
| 核验 | inspect 前误答率、locator 继承错误率、claim eligibility 错误率 |
| 覆盖 | full-asset 误判完成率、未核验候选遗留率 |
| 输出 | factual correctness、citation 合法率、证据范围外推率 |
| 效率 | 输入/输出 token、首字延迟、总耗时、工具调用数 |
| 稳定性 | 多次运行方差、answer review 重试率、超时率 |

### 归因顺序

bad case 必须依次检查：

1. system prompt、Skill 和 schema 是否正确送达；
2. reasoning、content 和 tool call 是否被 parser 正确解析；
3. 目标 Skill/reference 是否加载；
4. 模型是否采用已加载规则；
5. Runtime 是否执行应有门禁；
6. 工具结果是否充分且一致；
7. 最终生成或 answer review 是否引入退化。

只有问题属于程序性决策知识缺失或表达不清时才修改 Skill。字段合法性、状态转换、权限和可确定
顺序问题优先修 Runtime/schema。

读取次数不等于采用：根据工具参数、计划修订、证据核验和最终结论判断规则是否生效。
缺少事件证据时标为不可判定，不推测模型内部理解。原始检索、模型输入投影、事件与评测保持分层核对。
评分或 answer_review 失败不直接等于答案错误，保留原始答案与定位供独立评估。

## 十二、晋级与回退标准

B 版本替代当前 A 版本前必须满足：

- 所有证据、来源范围、coverage 和 citation invariants 保持；
- Qwen3.6-35B-A3B 关键题集无不可接受回归；
- 普通 QA 输入 token 或无效工具调用有可测量下降；
- full-asset 误判完成率不升高；
- candidate 直接进入答案的比例不升高；
- citation 合法率不下降；
- 实际加载内容或无效调用减少，必要 reference 漏读率不升高；不要求 reference 调用次数一定下降；
- answer review 重试、超时和多次运行方差不恶化；
- 若验证 Qwen3.8，其问题能够定位到明确层次，不能只报告总分；未验证不得宣称该路由适配完成。

首轮小样本只支持候选筛选，不宣称统计显著或长期稳定。报告逐题 A/B 多轮结果、正确性与证据判断、
关键规则采用情况和成本差异。聚合提升不能抵消可复现的阻断性回归；无法归因时不继续堆规则。
晋级前复核尚未覆盖的关键场景；仅文档题验证通过不足以替换全模态生产 Skill。

以下属于阻断性回归：

- 使用 raw search/catalog 内容支持最终事实；
- 丢失用户指定来源；
- 局部证据外推为全量结论；
- 非法、伪造或错误归属引用；
- 未核验候选被标记为 verified；
- 通过新增 fallback 掩盖真实失败。

若 B 未达到晋级条件，保留 A 为生产版本，并根据轨迹判断是调整 B 的组织方式、进入小范围 C
实验，还是终止本轮优化。不得因为已经投入改写成本而降低准入标准。

## 十三、预期结果

本设计预期得到的不是单纯更短的文档，而是：

- root Skill 成为可扫描、能根据证据回访和修订的 QA 决策协议；
- 普通 QA 不再承担全量视频、复杂冲突和 sandbox 计算细节；
- Qwen 对长指令优先级解析和隐式状态记忆的依赖降低；
- Qwen3.6、Qwen3.8 与闭源模型共享同一个语义基线；
- Runtime 条件加载是否必要能够由轨迹证据决定；
- 后续每次新增规则都有明确归属、评测依据和移除条件。

## 十四、首批迁移范围

本批保留旧 reference 文件名，避免与已有评测的 reference 路由统计变更混合。
主文件由 420 行缩至 341 行，新增两个直接入口；暂时共有十份 reference，不宣称已经完成
第五节以后规划的全部文件合并或约 200 行目标。原始主文件另存于本地
`.local/qa-skill-reorg-a0/SKILL.md`，原 references 可从修改前 Git 版本恢复。

| 原内容 | 当前权威位置 | 主文件保留的入口/边界 |
|---|---|---|
| 完整计划操作、目标替换、检索批次绑定 | `references/answer-plan.md` | 原问题、主动修订、证据与状态区分、最终自查 |
| 全量目录逐页审计、集合 identity/quantity | `references/full-asset-coverage.md` | full_asset 触发、候选不是总量证明；集合不自动等于全篇 |
| 数值输入绑定与集合求和 | `references/sandbox-computation-rules.md` | 合格输入、execute、不能计算检索摘要中的值 |
| 多模态工具能力细节 | `references/evidence-modality-policy.md` | 事实模态、语音不可从画面猜测、能力不明才读专项 |
| 视频 locator、采样默认值、重复核验 | `references/video-candidate-audit.md` | 核验前读取、不得伪造范围、主 Agent 综合 |
| 输出通用底线 | `SKILL.md` | 结论先行、术语与单位、精确引用、最终消息不伴工具调用 |

专项段落主要逐字迁移，尚未做全面去重；先保证可追踪迁移，再考虑合并为少量场景协议。
已移除 reference 间的强制串行读取依赖，加载入口统一留在主文件。
旧 `video_counter` 字段说明、模板的无条件不完整声明、计算 reference 的心算建议等已有冲突
尚未作为行为修复处理，须在共同基线清理时单独核对；不得把当前候选称为已上线或已验证有效。
本批不修改 maintenance invariants、工具、模型、Harness 或现有配置；不新增场景示例。
