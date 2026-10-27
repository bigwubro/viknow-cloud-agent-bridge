# DeepAgents 与 ADK 知识库问答对照

对照时间：2026-10-27 00:38–00:40（Asia/Shanghai）。Playground `http://127.0.0.1:5198`，进程 pid `35671`，启动于 00:29:28（Asia/Shanghai）。`VIKNOW_USE_SCRIPT_MODEL` 未设置。

两份 profile 各自维护。DeepAgents 用目录 `expert_buddy`，对外 id 现为 `expertbuddy-deepagents`。ADK 用目录 `adk_buddy`，对外 id 现为 `expertbuddy-adk`。下表记录的是当时的对外 id。技能仍分别是 `knowledge-qa-v2` 和 `knowledge-qa`。对照使用同一知识库 `e2e-artifacts-20261022` 和同一组问题，两份 YAML 并不相同。

## 结果

| 智能体 | 问题 | 状态 | 耗时(s) | 工具 | ViKnow | 20261022 |
| --- | --- | ---: | ---: | --- | --- | --- |
| deepagents | 文档写了什么 | completed | 26.27 | `read_file`、`knowledge_search`、`document_inspect`、`stop_gate_check` | 是 | 是 |
| adk | 文档写了什么 | completed | 24.07 | `knowledge_discover`、`knowledge_search` | 是 | 是 |
| deepagents | 视频左上角有什么 | completed | 18.04 | `knowledge_discover`、`knowledge_search`、`answer_plan`、`stop_gate_check` | 是 | 是 |
| adk | 视频左上角有什么 | completed | 2.01 | `knowledge_discover`、`list_video_segments` | 是 | 是 |
| deepagents | 文档验收短语是什么 | completed | 46.10 | `read_file`、`knowledge_discover`、`query_refine`、`knowledge_search`、`answer_plan`、`list_doc_segments`、`document_inspect`、`stop_gate_check` | 是 | 是 |
| adk | 文档验收短语是什么 | completed | 8.03 | `knowledge_discover`、`knowledge_search` | 是 | 是 |

六轮都完成。DeepAgents 平均 30.14 秒，ADK 平均 11.37 秒。ADK 没有调用 Actio，也没有跑回答检查。

## 文档写了什么

问题：`ViKnow索引验收20261022文档写了什么`。

检索片段的正文只有标题两行：`# Index smoke` 和 `ViKnow 索引验收 20261022 文档`。片段前面的 context 是索引器生成的说明，写了稳定性、上线标准和冒烟测试。

DeepAgents 先读技能，再检索，被 stop gate 拦住后调用 `document_inspect`。最终答案只保留页面上的两行，并说明其余为空白。session `fe3ec6d5-496b-49fa-adc6-c43884c8d0cc`，run `8cdd6ad9-2733-4870-b247-828be03c5abf`。

ADK 发现资料后检索，把 context 里的稳定性、上线标准和冒烟测试写成了文档正文，并带上了视频画面描述。关键词命中，正文超出了页面上的两行。session `cdebfcce-78e4-4ce0-aa1d-5636a85067bd`，run `934047ac-e00b-4d56-9678-7568b964e650`。

## 视频左上角有什么

问题：`视频左上角有什么`。

索引片段写明：0 到 3 秒纯黑背景，左上角白色文本 `ViKnow` 和 `20261022`，并带 OCR `ViKnow; 20261022`。

ADK 用 `list_video_segments` 读到这段文字，答案就是这两项。session `5a94da04-6b67-4270-9101-06f42c689dfd`，run `0c96b2b9-e199-4bf4-91d1-336bba7b1c5e`。

DeepAgents 检索到同一片段，stop gate 三次要求 inspect，最终没有调用 `video_inspect_2_0`。答案里的事实与片段一致，同时把闸门要求的核验过程写进了给用户的正文。session `74fedb38-5ff9-48b2-af8a-b4fcefd5cdd6`，run `2fcaf617-16ef-418d-888f-2fa940d869db`。

## 文档验收短语是什么

问题：`文档验收短语是什么`。

ADK 把检索到的标题行当作短语，并写了证据 id `chunk-8dca609df47870528caaa5264380d5e1`。session `cf81b379-dcde-47a8-950b-c3c5f12b8dd7`，run `a28c0ab5-1cd5-4be0-96eb-8a6cbfe91520`。

DeepAgents 检索并做了 `document_inspect`，结论是库里没有「文档验收短语」这个标准术语，同时提到了标题为验收记录的文档和视频上的 `ViKnow 20261022`。session `42abfcf7-aba5-422b-add6-9ba737c9baeb`，run `c63247ed-7f78-462b-a7ee-37f7c806f3f8`。

## 怎么读这张表

关键词两列只说明答案里出现了 `ViKnow` 和 `20261022`。文档题上，页面检视比检索 context 更紧。视频题上，两边都说到了左上角的两段文字；ADK 直接用片段，DeepAgents 的技能要求 inspect，这一轮没有走到 `video_inspect_2_0`。

## 复测

2026-10-27 09:03（Asia/Shanghai）重启 Playground，pid `700731`。ADK 的工具把 `### context` 从原文里拆掉，结果里只留 `chunk_text`。

文档题不再写稳定性、上线标准或冒烟测试。答案给出的文档内容是标题行 `Index smokeViKnow 索引验收 20261022 文档`。同一轮检索还带回了视频片段，答案末尾多写了该片段里的画面描述。session `5e94b5f8-e09f-4df0-b04c-592d4127b6d7`，run `5711ab84-8485-4a7f-9031-03e019773b1c`。

视频题仍是左上角白色 `ViKnow` 和 `20261022`。session `c52e0a57-aeb0-4d92-a7d0-44603f7a5113`，run `b2a100d0-5e87-420b-b894-505ad096fbe2`。

短语题给出标题行 `ViKnow 索引验收 20261022 文档`。session `cff59f70-1a7b-4726-a5d8-fe05cedac560`，run `7841a4fc-9215-44c5-a430-10671e1f68ff`。
