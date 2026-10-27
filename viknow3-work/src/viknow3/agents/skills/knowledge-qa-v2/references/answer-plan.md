# Answer Plan Operations

计划属于当前run。content记录目标、进度、冲突、待核验事项和下一步；findings逐条记录结论与证据。
原始用户要求独立保留，修改计划不能替换它。计划、findings和review都是Agent记录，不是系统核验证书。

## 完整调用

这里是完整正反示例的唯一维护位置；Harness遇到确定性错误会自动附回相关说明。
一个工具四种action，不传旧update对象、slot_updates、状态枚举或自评表单。

start/update可加唯一窄字段template_id：generic为通用，comparison为对比或同对象前后观察。
省略保留当前选择，显式generic回退；view/review不选模板。切换模板是计划实质变化，须重新收尾。
选择成功的工具结果直接附回canonical核心模板；完整格式例仅维护在answer-templates.md，
无需额外read，读reference本身不会激活。说明加载失败时整个写入不提交，旧计划与选择保留。

```json
{"action":"update","template_id":"comparison","content":"比较同一对象前后观察，以情绪等共享属性为行；结论另列。","findings":[]}
```


- view：只查看，省略content与findings。
- start：首次建立content与findings，已有正文改用update。
- update：完整替换content与findings；省略findings表示清空，保留有效记录时须一起提交。
- review：content记录收尾说明；省略findings保留现有记录，显式提供则完整替换，再对新版本收尾。

正文或findings任一变化，都产生新版本并使旧收尾失效。相同更新不制造变化。
只在调查方向、关键结论、冲突或范围缺口发生实质变化时更新；不要求每次inspect后写入，
可在一组检查完成后集中记录。不要把每次工具调用或已有证据全文复制进计划。
findings直接传JSON数组，不把数组再编码成字符串；text中的引号按普通JSON字符串转义。

```json
{"action":"start","content":"目标：核对地区代码。下一步：搜索并inspect。"}
```

```json
{"action":"view"}
```

尚未取得可用证据，只记录待办，不虚构标签：

```json
{"action":"update","content":"搜索候选显示地区代码可能为SC，尚未inspect确认。下一步检查原页。","findings":[]}
```

下面仅当工具实际返回[C1]且支持此处结论时使用；每条结论分别绑定，不用全局引用数组：

```json
{"action":"update","content":"已核对地区代码；继续调查安装前置条件。","findings":[{"text":"中国大陆对应的地区代码为SC。","claim_refs":["[C1]"]}]}
```

```json
{"action":"review","content":"代码已核对，其他安装条件未查明，将限定回答范围。"}
```

review不会生成答案，之后单独调用无参数final_check()。无需每次格式重试重复review。
content最多6000字符，findings最多40条，每条text最多2000字符；长笔记放当前run沙箱文件。
计算仍使用执行工具写代码，数值和组成关系需有证据，计划不是执行记录。

### 合并错误反例

假设当前可用目录只有[C1]。以下同一例有多类错误：正文手写标签、finding正文写引用、
不存在的[C99]、重复键、内部长引用。任何一项都会拒绝本次写入，原计划不变。

```json-invalid
{"action":"update","content":"已核实[C1]","findings":[{"text":"代码为SC [C1]","claim_refs":["[C99]","[C99]","claim:内部长引用"]}]}
```

正确方式是把干净结论写在text，真实短键只放该条claim_refs；目录为空时改记待核验事项。
模型不能自行编号，也不能因为看见示例就认为本题已有[C1]。

## 集合目标

content明确计数单位、范围、去重依据与遗漏；findings逐条关联已取得的证据。
不要填写collection、verified_items或catalog_reviews对象，不能用计划声明替代工具实际覆盖。

## 比较组织

比较任务仍使用同一套四action和content/findings，不新增模式或自评字段。
同对象前后比较时，双方是前期/后期实际观察，维度为情绪等共享属性；对象比较时双方是对象。
相同状态允许相同表述，但引用仍对应各自scope，不能复制整体概括冒充两次观察。
content简要保留比较双方、候选共享维度、调查安排及实质变化；不是固定表格或事实证书。
findings逐侧记录实际观察，每条仅绑定支持该观察的真实claim_refs；未核实的一侧写入content缺口，
不为了补齐表格伪造finding。review的content会成为closing_note，收尾时保留当前维度、
未核实侧、已覆盖范围及剩余缺口。最终生成据此组织Markdown表格；计划只组织和导航证据，
事实仍以当前可用目录的claim及scope为准。没有明确组织说明时按原题合理组织，不因此阻断回答。

以下是同一虚构格式案例，不是本题证据：比较样例甲乙的动作与时段；假设工具实际返回[C1]，
其完整claim只支持样例甲在已核验时段使用餐具，乙侧尚未核实。

正确：组织说明与逐侧证据分开，缺口保留到收尾。

```json
{"action":"update","content":"比较样例甲乙；共享维度为动作、时段。甲侧已有观察，继续核验乙侧及其余时段。","findings":[{"text":"样例甲在已核验时段使用餐具。","claim_refs":["[C1]"]}]}
```

```json
{"action":"review","content":"按动作、时段比较甲乙；甲侧仅覆盖已核验时段，乙侧未核实。结束调查后保留乙侧与其他时段的覆盖缺口。"}
```

合并反例：把正文标签和全局证据混在计划中，把甲侧证据借给乙侧，并在收尾抹去未知范围。
其中标签格式会被Harness拦截，证据是否支持结论仍由Agent负责，不能期待格式校验保证事实正确。

```json-invalid
{"action":"update","content":"甲乙比较已完成[C1]。","findings":[{"text":"样例甲使用餐具[C1]，所以乙侧没有同类动作，整个视频均如此。","claim_refs":["[C1]"]}]}
```

不要把“乙侧未核实”改成“不存在”，也不要因计划写过某结论就把它当作已核验来源。

## 证据标识

content、finding.text及review说明都不手写[C1]、[H1]、[1]、[[1]]等引用标签或内部长ID。
每条finding必须有非空claim_refs，只接受当前可用目录中的实际短键，不重复、不自行编号。
先等待inspect结果返回，再在下一次调用写入相应finding；不能猜测同批inspect将产生的标签。
Harness只检查格式和身份存在性，不保证证据支持结论。预编译知识不可冒用inspect标签；
其允许用途及局限可记在content，尚未建立独立来源身份时不伪造findings绑定。
已收到自动附回说明就按说明纠正，不必重复读reference或原样重试。
