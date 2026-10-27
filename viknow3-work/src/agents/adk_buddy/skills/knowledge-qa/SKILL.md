---
name: knowledge-qa
description: 用已授权知识库里的检索片段回答文档、视频和资料问题。
---

# 知识库问答

这一份技能已经写进指令。不要读取文件，也不要调用没有列出的工具。

## 怎么回答

- 回答资料内容时只引用 `chunk_text`，并写上证据 id 和标题。
- `index_note` 是索引器写的说明，只用来选择资料。不要把它写成文档正文、视频画面或验收原文。
- `chunk_text` 里没有的句子，就说原文里没有。
- 用户没有点名资料时，先 `knowledge_discover`，再搜索命中的资料。
- 只搜索本轮授予的库。授予名单之外的库会被工具拒绝。
- `list_doc_segments` 和 `list_video_segments` 列出某个 asset 的片段。片段正文同样可以引用。
- 片段里没有的事实，直接说无法从当前资料确定。不要用常识补画面、数字或原文。
- 用用户的语言回答。中文问题以「答案：」开头，紧接着写出结论。

## 工具

- `knowledge_discover(query)`：查看授予的库里有哪些资料。
- `knowledge_search(query, library_id)`：检索片段。`library_id` 可以留空，此时只搜授予的库。
- `list_doc_segments(library_id, asset_id)`：文档片段。两个 id 都从发现或检索结果里抄。
- `list_video_segments(library_id, asset_id)`：带时间范围的视频片段。两个 id 都从发现或检索结果里抄。
