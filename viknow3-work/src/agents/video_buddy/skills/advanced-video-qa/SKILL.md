---
name: advanced-video-qa
description: adaptive visual fact extraction from one selected knowledge-base video clip.
allowed-tools: knowledge_discover query_refine knowledge_search list_video_segments video_inspect_2_0 document_inspect image_inspect text_inspect
version: 0.3.0
---

# Advanced Video QA

Use this workflow to evaluate `video_inspect_2_0`.

1. Keep the original question and option meanings unchanged. Identify the condition that
   distinguishes the remaining options. Simple questions need no query_refine call. For a
   complex question, refine at most once, then reuse the original conditions and existing slots.
2. If a usable candidate is already available, inspect it. Otherwise discover the required
   scope and search for a locator. Search text/captions locate evidence; they do not verify it.
   Copy the candidate's library_id, asset_id, evidence_id and time_range exactly. Start a
   decisive inspect before spending more calls paraphrasing search or paging the same directory.
3. Choose each check for a concrete gap. Reuse completed evidence. Rewording a check of the
   same detail in the same window adds no independent evidence. Change the endpoint, crop,
   modality or sampling only when it can resolve a stated ambiguity.
4. Start with fps=auto and visual_budget=balanced. For hands, occlusion, material, direction,
   or text, focus on the smallest useful region and appropriate fps. AVI may perform one
   internal refinement; include its cost in the same run budget.
5. Compare changes using both endpoints and their own absolute times/sources. For event order,
   retain every requested event; locate the anchor before first/after/before checks. Use
   list_video_segments for legal adjacent ranges; gaps remain unknown. Clothing alone cannot
   establish or disprove person identity. Spoken mentions require speech/subtitle evidence.
6. Preserve supporting and conflicting claims. Answer using exact citation tokens and
   original option IDs. After evidence, include a standalone Current choice: <letter> line
   in the normal decision, or Current choice: unknown to withdraw an unsupported choice.
   If run_progress is offered, attach supporting/conflicting claim refs and remaining conditions
   to the normal tool call. These checkpoints are provisional, not evidence; add no extra turn.
7. On repeated unchanged searches, inspect a decisive candidate or change a locating
   condition. Do not replace blocked searches with repeated refine or catalog map/page calls.
   Directed catalog select remains available to locate an anchor or legal adjacent interval.
   When exploration closes, finalize from existing evidence within the original
   budget. For required single choice, select the best available option, label uncertainty,
   and end with Final answer: <letter>. Never fabricate a successful check or citation.

AVI extracts observations from one selected window; integrate separate windows using their
own provenance. Do not infer unseen intervals or hidden objects from incomplete observations.
