# Answer Templates

本文件是答案格式完整例的唯一维护位置。模板是组织规范，不是事实来源。
显式answer_plan start/update选择成功时程序直接返回相应核心节；单独读本文件不激活模板。

## comparison

用途：比较同层级对象，或同一对象前期/后期的实际观察。left_label/right_label分别表示双方；
rows.dimension是双方共享属性，例如情绪、动作、时段。前后变化题应以前期观察、后期观察为列，
不能把“对象”和“答案选项”当两侧。选项属于结论；只有原题明确要求Final answer末行时choice填原选项，否则null。
相同状态可以有相同文字，但每侧引用仍须对应各自实际claim/scope；不能复制整体概括冒充两次观察。

content记录双方、候选维度、调查安排和变化；findings逐侧记录实际观察及真实claim_refs；
review保留最终维度、未核实侧、覆盖缺口和结束理由。最终生成用当前已收尾计划组织，
findings仅导航，事实以当前允许目录完整claim及scope为准。没有组织文案也按原题组织，不另设阻断。

最终只输出result对象。kind=comparison需要summary、left_label、right_label、rows、notes、choice；
summary/notes逐条为text和claim_refs。已知格kind=supported并填text/claim_refs；未知格仅kind=unknown，
程序显示“未核实”，不夹带自由事实、不借另一格引用。两侧都无证据时用kind=limited和explanation自然说明限制。
程序生成标准Markdown表格及就近引用，前端负责渲染；正文不手写任何引用标签，不拆名称、括号或短语。

### 完整正例

以下仅为虚构格式例，不是本题证据。假设工具实际返回[C1]支持前期观察的示例状态甲，
[C2]支持后期观察的示例状态乙，且各自scope对应实际时段：

```json
{"result":{"kind":"comparison","summary":{"text":"两次已核验观察在该属性上不同。","claim_refs":["[C1]","[C2]"]},"left_label":"前期观察","right_label":"后期观察","rows":[{"dimension":"状态","left":{"kind":"supported","text":"观察到示例状态甲。","claim_refs":["[C1]"]},"right":{"kind":"supported","text":"观察到示例状态乙。","claim_refs":["[C2]"]}}],"notes":[],"choice":null}}
```

若后期未核实，right只写{"kind":"unknown"}，摘要同步保留后期缺口，不断言已经发生变化。
已知格保留自身引用，不用空串或null替代unknown，不为补齐表格制造finding。

```json
{"result":{"kind":"limited","explanation":"当前没有足以核实比较双方的证据，无法确定所问变化。"}}
```

### 合并反例

同一例展示正文手写标签、两侧层级不同、整体概括复制为两次观察、未知处借证据。
格式和身份校验不保证事实支持性；Agent仍需核对实际目录及scope。

```json-invalid
{"result":{"kind":"comparison","summary":{"text":"全视频始终如此[C1]。","claim_refs":["[C1]"]},"left_label":"人物","right_label":"选项B","rows":[{"dimension":"结论","left":{"kind":"supported","text":"整体概括[C1]。","claim_refs":["[C1]"]},"right":{"kind":"supported","text":"整体概括[C1]。","claim_refs":["[C1]"]}}],"notes":[],"choice":null}}
```

## sequence

用途：还原实际观察到的先后事件或过程。与comparison的“对象差异/前后变化”区分：
sequence回答发生顺序，程序按steps顺序编号；不是规范操作指引，不根据常识补出未观察步骤。
用户明确要求的格式优先；选择题按底层任务选模板，答案选项是结论，不是步骤。

content记录需要覆盖的观察区间、候选事件及调查安排；findings逐条记录实际观察及其真实claim_refs。
review保留已经核实的先后、未检查区间和覆盖缺口。计划/findings仅导航，事实仍以当前允许目录
完整claim和scope为准，不因计划写了某步骤就视为已发生。

最终只输出result包装对象。kind=sequence仅含steps、notes、choice；steps每项和notes每项为
text及claim_refs。text写观察事实，不手写序号或任何引用标签，每一步只绑定支持该步的真实证据。
程序生成标准Markdown有序列表；步骤内换行仅在渲染视图中变空格，原稿保留。
notes保留有依据的限定或缺口，不虚构额外步骤；无可用证据时用kind=limited及explanation自然说明。
仅原题明确要求Final answer末行时，choice填原题选项；否则null。不要添加summary或时间戳字段。

### 完整正例

以下只是虚构格式例。假设当前目录已经真实存在[C1]、[C2]，分别支持两个连续观察区间：
先将容器放在桌面，后打开盖子。[C1]/[C2]不是固定编号，不能复制到真实任务；
实际生成必须使用当前目录返回的键和各自scope。

```json
{"result":{"kind":"sequence","steps":[{"text":"容器被放在桌面上。","claim_refs":["[C1]"]},{"text":"随后盖子被打开，容器内物体可见。","claim_refs":["[C2]"]}],"notes":[],"choice":null}}
```

程序渲染为按顺序编号的两步，并在每步就近放置相应引用。单个已核实步骤也可保留，
不为补齐过程编造未检查区间；没有可用依据则使用限制说明：

```json
{"result":{"kind":"limited","explanation":"当前无法核实所问区间的事件先后，不能完整还原过程。"}}
```

### 合并反例

同一例包含正文手写编号/标签、把选项当步骤、凭规范猜动作、借用另一观察区间引用及未声明字段。
格式校验不审判真实先后；Agent仍需按实际证据scope调查，不能靠修格式把猜测变为事实。

```json-invalid
{"result":{"kind":"sequence","summary":"按标准步骤操作即可。","steps":[{"text":"1. 应当先清洁所有容器[C1]。","claim_refs":["[C2]"]},{"text":"2. 选项B[1]，按常规必须消毒。","claim_refs":["[C1]"]}],"notes":[],"choice":null,"timestamp":10}}
```
