# Knowledge QA v2 模型与 Skill 优化指导

## 目的与适用范围

本文用于指导 `knowledge-qa-v2` 后续的结构优化、模型适配和评测设计，不是运行时协议，
不得由 QA Agent 作为回答规则加载。当前主要模型基线是本地部署的
`Qwen3.6-35B-A3B`；`Qwen3.8-Flash-Next` 是待评估候选；闭源前沿模型作为能力与
编排策略的对照组。

本文不预设新模型优于旧模型。尤其是，现阶段有限人工体验显示，当前 Skill 与
`Qwen3.8-Flash-Next` 配合时没有稳定优于 `Qwen3.6-35B-A3B`。这只是一项需要通过
配对实验验证的项目观察，不能外推为模型通用能力结论。

## 当前结论

后续优化应遵循以下总原则：

> Skill 承载少量、聚焦、可执行的程序性知识；按需材料只设一层 reference；字段约束、
> 状态转换和证据合法性尽量由 Runtime 强制；每项结构调整必须分别在目标模型上做配对评测。

优化目标不是单纯缩短 `SKILL.md`，而是让进入当前上下文的每条指令都影响当前决策，并使
模型不必依靠长上下文记忆来模拟精确状态机。

## 社区研究所支持的经验

### Skill 是程序性知识，不是大号系统提示词

SkillsBench 将 Skill 定义为适用于一类任务的程序性知识包，包含工作流、领域约定、模板、
示例和可执行资源。事实检索、工具能力说明和平台级约束与 Skill 有关，但不应全部复制进
`SKILL.md`。

对 ViKnow，可按以下边界组织信息：

| 信息 | 首选归属 |
|---|---|
| Skill 的适用条件 | description 与短 root Skill |
| QA 的核心语义决策 | root Skill |
| 特殊场景的处理方法 | 一层 `references/` |
| 参数、字段和类型合法性 | tool schema / Pydantic 模型 |
| 工具顺序、状态转换、权限和预算 | Runtime / Harness |
| 领域事实 | 知识库与检索结果 |
| 精确计算和机械转换 | tool / script |
| 输出正确性和行为回归 | verifier / gate / eval |

### 人工整理的 Skill 有效，自生成 Skill 不可靠

SkillsBench 在多个商业 agent-model 组合上的实验显示，人工整理的 Skill 平均显著改善任务
通过率，但收益随模型、Harness 和任务而变化；让模型在任务开始时临时生成 Skill，平均没有
收益。部分任务加入 Skill 后反而退化，说明 Skill 必须通过配对实验验证，不能假设增加规则
一定有益。

这支持 ViKnow 当前的受控优化方向：候选 Skill 可以由模型辅助产生，但必须经过人工审查、
确定性检查和真实轨迹评测后才能进入生产版本。

### 聚焦、适量优于全面详尽

SkillsBench 的分组分析显示，单任务提供 2～3 个聚焦 Skill 的收益高于提供 4 个以上 Skill；
compact 和 detailed 类型整体优于 comprehensive 类型，后者在该实验中出现负收益。该结果
不能直接给出 ViKnow 的最佳行数，但足以否定“覆盖越全面越可靠”的默认假设。

因此，不应继续通过向 420 行 root Skill 追加重复规则来修复 bad case。新增规则前应先判断：

1. 它是否属于真正的跨任务语义不变量；
2. 是否已经由 Runtime 或 schema 强制；
3. 是否只服务某一种问题路径；
4. 是否可以用结构化状态或错误码代替自然语言提醒；
5. 是否会与已有绝对规则形成例外或冲突。

### Progressive Disclosure 有效，但不是越深越好

近期研究表明，渐进式加载能够改变 Agent 实际访问和采用程序知识的方式，并在部分任务上
改善结果；对大型、多轮 Skill，它还能明显降低有效输入量。但研究没有发现一种对所有任务
都最优的加载方式。

长文档研究进一步显示，一层 progressive disclosure 在跨多个大型资源时有价值，而增加第二层
路由没有带来稳定收益，有时会降低准确率。其核心收益是节省上下文和改善导航，而不是提升
模型本身的推理能力。

ViKnow 应采用：

```text
Skill description
  → 只负责发现和激活

SKILL.md
  → 核心不变量和一层决策协议

references/
  → 按当前问题路径加载的专项协议
```

不应继续建立 reference 内部的多级索引或依赖模型沿多层链接自主导航。

### Harness 行为与模型行为需要分别观测

不同 Agent 平台可能由模型主动读取 Skill，也可能由 Harness 注入 Skill；前置元数据、reference、
上下文压缩和工具权限的处理并不一致。因此，文件内容正确不等于运行时已加载，也不等于模型
实际采用。

ViKnow 的评测至少要区分：

1. Skill 是否被激活；
2. 所需 reference 是否被读取；
3. 读取后的规则是否在工具轨迹中得到采用；
4. 错误行为是否被 Runtime 拦截；
5. 最终答案是否因该规则而改善。

## 模型差异与适配策略

### 共同底线

不同模型必须共享同一套领域语义，不允许通过模型专用 Skill 改变以下定义：

- 合法证据来源；
- answer slot 与 requested evidence scope；
- candidate、inspect claim 与 final claim 的边界；
- claim eligibility；
- local 与 full-asset coverage；
- citation contract；
- 证据不足、歧义和冲突的表达标准；
- 最终质量评价标准。

模型适配只能调整协议的展开程度、reference 注入方式、恢复提示、规划自由度和调用预算。

### Qwen3.6-35B-A3B：当前基线

Qwen 官方将 Qwen3.6 开放模型定位为强化了 agentic coding、长任务稳定性和 thinking preservation
的模型系列。ViKnow 当前使用的是 `Qwen3.6-35B-A3B`，不是 27B dense 版本；后续报告和实验必须
记录完整 checkpoint 与部署参数，不能用笼统的“Qwen3.6”代替。

对该模型，建议采用“窄 Skill + 强 Runtime”：

- root Skill 使用短句、显式优先级和有限状态路径；
- 用封闭白名单定义合法最终事实来源，避免“绝对规则 + 分散例外”；
- 每个阶段明确输入、动作、成功条件和失败条件；
- 关键状态由 Runtime 保存并在需要时回显；
- locator lineage、claim ref、coverage、引用和重复调用由代码校验；
- reference 加载不能完全依赖模型自主判断；
- 为最容易出错的边界保留少量正反例；
- 工具失败时返回稳定 reason code 和少量合法下一步，不给开放式长建议。

推荐让模型主要负责语义工作：理解问题、定义 answer slots、生成检索 query、判断候选相关性、
综合合格 claims 和生成自然语言答案。精确状态机不应由模型通过阅读长文档自行模拟。

### Qwen3.8-Flash-Next：候选模型

Qwen3.8-Flash-Next 是近期发布的开放权重多模态 MoE 模型，主模型约 125B 参数、每 token 激活约
6B，并引入 GDN、QSA、Gated Residual 和 N-gram Embedding 等面向长上下文效率的新结构。官方
定位强调成本效率、编码和办公任务能力，但这些指标不能直接证明它更适合 ViKnow 的长 Skill、
多工具知识问答和证据约束。

其每 token 激活规模较小且架构变化较大，因此不应根据总参数、上下文长度或通用 benchmark
预测 QA Skill 表现。当前 Skill 没有与它配合得更好，可能来自多个层次：

- root Skill 过长，关键规则竞争注意力；
- 稀疏/高效架构对长指令中的远距离约束采用方式不同；
- thinking 或 reasoning context 的保存配置不匹配；
- chat template、reasoning parser 或 tool-call parser 与模型不匹配；
- schema 过大或工具数量过多；
- Runtime feedback 在长轨迹中被弱化；
- 模型在检索规划、候选审计、工具参数生成或最终综合中的某一环节退化。

这些都是待检验假设，不能只通过修改 Skill 文案判断。正式比较时必须固定或记录：

- 精确模型 checkpoint 和量化方式；
- serving engine 与版本；
- chat template；
- reasoning parser 和 tool-call parser；
- thinking 模式及历史 thinking 是否保留；
- temperature、采样参数和最大输出；
- 工具 schema 版本；
- 上下文压缩与缓存策略；
- Runtime、知识库快照和 Skill 内容哈希。

在得到证据前，Qwen3.8-Flash-Next 应作为独立候选轨道，而不是 Qwen3.6-35B-A3B 的直接替代。

### 闭源前沿模型：对照组

公开评测表明 GPT、Claude 和 Gemini 的前沿模型仍能从人工 Skill 获得明显收益。更强模型通常
能处理更抽象的原则、按需加载和动态规划，但不代表适合更长或更矛盾的 Skill。OpenAI 官方
模型指导也明确提醒，强模型虽然更能遵循长指令，却可能对上下文中的模糊和冲突更加敏感。

对闭源前沿模型，可以评估：

- 更短、更原则化的 root Skill；
- 更高的 reference 自主加载权；
- 基于 gap 的动态第二轮检索或核验；
- 更丰富的自然语言 specialist handoff；
- 在预算内自主选择 discriminating check。

但安全权限、证据合法性、locator lineage、full-asset completion、citation 和总预算仍应由
Runtime 强制，不能因为模型更强而退回纯 Prompt 保证。

## 推荐的目标结构

不要长期维护相互独立的 `knowledge-qa-qwen` 和 `knowledge-qa-frontier` 两套领域协议。推荐维护
一个权威语义核心，并由 Harness 根据模型策略控制展开程度：

```text
统一 QA 语义
├── Runtime 不变量与状态机
├── 精简 root Skill
├── 一层场景 references
└── Model Policy
    ├── Qwen3.6-35B-A3B：显式步骤、较强约束、条件注入
    ├── Qwen3.8-Flash-Next：独立评测后确定展开和预算
    └── 闭源前沿模型：原则化提示、较高动态规划自由度
```

Qwen 路径的初始目标可以设为：

- root Skill 约 180～220 行，仅作为实验起点而非硬性标准；
- 顶层不可变规则不超过 8～10 条；
- 4～5 个主阶段；
- 一项规则只有一个权威定义位置；
- 普通事实问答通常加载 0～1 个 reference；
- 保留普通文本事实、视频单帧计数、全资产枚举三类短例子；
- Runtime 能确定的 reference，由 Runtime 条件提示或注入。

闭源模型可以额外测试约 100～160 行的原则型 root，但不应在实验前把它确定为生产目标。

## 优化实验设计

### 组织变体

在修改生产 Skill 前，至少构造以下语义等价变体：

| 变体 | 组织方式 |
|---|---|
| A | 当前 420 行 root Skill |
| B | 约 200 行 root + 一层按需 references |
| C | 约 200 行 root + Runtime 条件提示或注入 reference |
| D | 约 100～160 行原则型 root，主要用于闭源模型对照 |

压缩必须保持领域不变量不变。若一个变体改变了证据来源、coverage 或终止策略，它属于行为方案
实验，不能作为纯 Skill 组织实验与其他变体直接比较。

### 模型矩阵

最低对照矩阵：

```text
Qwen3.6-35B-A3B    × A / B / C
Qwen3.8-Flash-Next × A / B / C
闭源前沿模型       × A / B / C / D
```

每个条件应多次运行，并按题目、模型、Harness 配对比较。不能只比较汇总均值，也不能只使用
单次人工体验。

### 任务分层

评测集至少覆盖：

- 单文档直接事实；
- 指定来源约束；
- source mismatch 和重新发现；
- 跨文档比较；
- 用户上传图片纯看图与知识库联动；
- 视频局部动作核验；
- 单帧可见物体计数或位置；
- 整段视频或整份文档的全量枚举；
- 多模态冲突；
- 计算型问题；
- 无证据、部分证据和真正冲突；
- 历史 claim 复用与新状态重新 inspect；
- 连续追问与独立新任务切换。

### 评价指标

最终正确率之外，必须记录过程指标：

| 层次 | 指标 |
|---|---|
| 激活 | Skill 激活率、错误激活率 |
| 加载 | reference 实际读取率、无关读取率、缺失读取率 |
| 采用 | 核心规则在轨迹中的 uptake、首次合法工具调用率 |
| 规划 | answer slot 覆盖率、来源范围保留率 |
| 检索 | 重复搜索率、有效候选率、无进展轮数 |
| 核验 | inspect 前误答率、locator 继承错误率、claim eligibility 错误率 |
| 覆盖 | full-asset 误判完成率、未核验候选遗留率 |
| 输出 | factual correctness、citation 合法率、证据范围外推率 |
| 效率 | 输入/输出 token、首字延迟、总耗时、工具调用数 |
| 稳定性 | 多次运行方差、answer review 重试率、超时率 |

### 归因顺序

出现 bad case 时，按以下顺序定位，不直接改 Skill：

1. 模型是否收到正确的 system prompt、Skill 和 schema；
2. parser 是否正确解析 reasoning、content 和 tool calls；
3. 目标 Skill/reference 是否被加载；
4. 模型是否理解并采用了已加载规则；
5. Runtime 是否执行了应有门禁；
6. 工具结果是否提供了足够且一致的信息；
7. 最终生成或 answer review 是否引入退化。

只有确认问题属于程序性决策知识缺失或表达不清时，才修改 Skill。若问题来自字段合法性、状态
转换、工具权限或可确定的顺序，应优先修 Runtime/schema。

## 变更准入规则

候选 Skill 进入生产前必须满足：

1. 没有改变既有证据与覆盖不变量，或该行为变化已经单独获批；
2. 没有通过新增 alias、兼容字段或兜底路径掩盖第一处设计问题；
3. Qwen3.6-35B-A3B 的关键任务不发生不可接受回归；
4. Qwen3.8-Flash-Next 的收益来自配对结果，而不是单次印象或通用 benchmark；
5. 闭源模型收益不能作为开放模型上线的替代证据；
6. token 与时延改善没有以牺牲 evidence correctness 或 coverage 为代价；
7. 失败轨迹能明确归因到加载、采用、Runtime、工具或生成中的某一层；
8. 更新对应 invariants、eval scenarios 和 optimization log。

## 近期执行建议

1. 先冻结当前 Skill 作为 A 基线，记录内容哈希和部署配置。
2. 修正现有 reference 与多环境配置中的陈旧合同，不与结构实验混在一起。
3. 建立语义不变量清单，区分 Skill、reference、schema 和 Runtime 的权威归属。
4. 生成保持语义等价的 B/C 变体，不先改生产默认版本。
5. 使用同一题集比较 Qwen3.6-35B-A3B 与 Qwen3.8-Flash-Next。
6. 选一个闭源前沿模型作为能力上界和原则型 Skill 对照。
7. 根据轨迹归因决定下一步是精简 Skill、增强 Runtime 条件注入，还是调整模型服务配置。

## 参考资料

- [SkillsBench: Benchmarking How Well Agent Skills Work Across Diverse Tasks](https://www.skillsbench.ai/skillsbench.pdf)
- [SkillJuror: Measuring How Agent Skill Organization Changes Runtime Behavior](https://arxiv.org/abs/2606.11543)
- [Skill Blocks: How Should an Agent Load Its Skill?](https://arxiv.org/abs/2608.14943)
- [Is Progressive Disclosure All You Need for Long-Context Agents?](https://arxiv.org/abs/2607.17598)
- [Agent Skill Implementation：跨平台 Skill 加载行为调查](https://github.com/agent-ecosystem/agent-skill-implementation)
- [Qwen3.6 / Qwen3.8 官方模型仓库](https://github.com/QwenLM/Qwen3.8)
- [Qwen3.8-Flash-Next 官方模型仓库](https://github.com/QwenLM/Qwen3.8-Flash-Next)
- [Qwen-Agent 工具调用说明](https://qwenlm.github.io/Qwen-Agent/en/guide/get_started/quickstart/)
- [OpenAI 官方模型提示与指令遵循指导](https://developers.openai.com/api/docs/guides/latest-model)

## 证据限制

- 当前缺少针对 ViKnow QA 工作流、相同 Harness 和相同 Skill 内容的跨模型公开对照研究。
- SkillsBench 的主体是开发与专业任务，结论不能无条件外推到多模态知识库问答。
- Qwen3.8-Flash-Next 发布较新，公开资料主要是官方架构和通用 benchmark，尚不足以判断其在
  ViKnow 证据编排中的真实表现。
- 因此，本文提供的是实验假设、组织原则和准入标准；最终模型选择必须由 ViKnow 自身的配对
  轨迹评测决定。
