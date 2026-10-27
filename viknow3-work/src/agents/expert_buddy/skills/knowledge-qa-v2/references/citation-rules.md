# Citation Rules

For knowledge-base facts, citations come from eligible current-run inspect
`claims` or valid historical verified claims. Search results and locator
outputs are never citable. A solely visible, user-uploaded image may instead
be answered from `uploaded_image_search.decision=answer_from_image` under the
root Skill's image-only condition; do not invent an inspect citation for it.
Conceptual claims explicitly attributed to ViKnow expert model are a separate
path, not a citation for specific indexed content.

`visual_ground` claims are citable only for visual grounding and object-location
facts. Its raw detections, extracted frames, sampled timestamps, and
search/locator inputs are not citable by themselves.

When the final answer gives a visible object/category count and a
`visual_ground` claim exists for that count, cite the `visual_ground` claim
beside the count sentence. Do not cite only the earlier `video_inspect`,
`knowledge_search`, or `list_video_segments` evidence for the same count.

## Current-Run Claims

Put the exact returned `agent_citation`, such as `[C1]` or `[C2]`, in the
corresponding unit's `claim_refs`. Keep `text` free of source labels; the
runtime inserts them. Use the same tokens in answer_plan findings[].claim_refs;
the runtime maps them to stable internal identities. Do not renumber, merge, or rewrite tokens. These C/H tokens identify
claims in model output; Runtime later merges selected same-source claims and
publishes single numeric references for users. Public numbers in a previous
answer are local to that answer; use the supplied historical reference mapping
to find the corresponding H tokens. Ask for clarification if the answer is ambiguous.

## Historical Claims

When using a historical verified claim, copy its stable `citation_token`
exactly as supplied in the historical context, for example `[H1]`. A raw
claim reference or internal claim ID is not a citation token.

## Claim Eligibility

Do not cite:

- failed tool results;
- claims with `confidence <= 0`;
- raw `visual_ground` detections, extracted frames, or per-frame results unless
  they were summarized into a returned claim;
- raw `knowledge_search` text, captions, indexed transcripts, indexed
  visible-text snippets, or `list_video_segments` content.

Before citing a claim, check that it supports the requested subject, source,
predicate, period, unit, attribute and scope. A valid token on a different
fact cannot make a substitute answer valid. If a required knowledge-base fact
has no eligible claim, say which part is unsupported; an insufficiency-only
answer needs no citation.

## Local unit binding

When an answer uses citations, every nonblank prose unit must bind supporting
claim_refs. Put a structural heading, transition or limitation in the same unit
as its related supported fact; preserve the text and line breaks. Keep separate
independently checkable facts in separate units, never merge the whole answer
or borrow an adjacent unit's references. The only unbound standalone prose
exception is a pure Final answer letter line explicitly requested by the original
question. An entirely uncited response may explain limitations; syntax validation
does not certify evidence support or factual correctness.

## Final submission

Call the no-argument `final_check()` alone when ready to answer. It generates
the structured candidate from the current full context. Do not directly emit
a final prose answer or add citation labels yourself.

## 结构化正文格式

以下规范仅用于final_check内部的完整正文生成及格式修复。

{{qa_final_answer_format_examples}}

## 绑定编辑修复

此调用只提交本次提供的编辑schema所要求的JSON，不输出完整units正文。
使用本次给定的可编辑索引、证据键与原稿；只能修改明确允许的绑定或操作。
保留未列入可编辑范围的正文和绑定，不借用其他单元的依据。
具体字段、必填编辑项和允许操作以本次schema与动态约束为准；
格式修复不能发明证据，不能把候选原稿当作事实来源。
