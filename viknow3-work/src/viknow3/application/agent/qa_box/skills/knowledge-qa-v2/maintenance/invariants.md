# Knowledge QA v2 Invariants

These invariants define the behavior boundary for `knowledge-qa-v2`. Automated
optimization may clarify or reorganize instructions, but must not weaken these
contracts. Changes to this file require explicit human review.

## Evidence Grounding

- Do not answer knowledge-base factual questions from memory, raw search text,
  indexed transcripts, captions, visible-text snippets, or segment lists.
- Treat `knowledge_search` and `list_video_segments` output as candidate
  locators only.
- Final factual claims must be grounded in eligible current-run inspect claims,
  valid historical verified claims, or an `uploaded_image_search` result whose
  decision is `answer_from_image`.
- If no eligible evidence supports a needed fact, state insufficiency instead
  of filling the gap with general knowledge or plausible inference.
- Distinguish direct evidence, locator evidence, expert-model support, and
  inference. Do not present one as another.

## Tool Ordering

- Discover or use an explicit knowledge library before knowledge-base search.
- If a user-uploaded image may affect a knowledge-base answer, run
  `knowledge_discover` first, then `uploaded_image_search` with the selected
  `library_id` when available.
- Do not call dependent tools in the same assistant turn when their arguments
  depend on earlier tool results.
- Inspect tools must run only after candidate evidence has been selected from
  `knowledge_search` or `list_video_segments`.
- Do not use `read_file` or direct model vision to inspect user-uploaded
  workspace images. Use `uploaded_image_search`.

## Verification

- Discard failed tool results, claims with `confidence <= 0`, and
  `text_inspect` claims where `quote_verified == false`.
- Inspect evidence by the modality required for the fact, not by the container
  that happened to retrieve it.
- Use transcripts, captions, visible-text snippets, and segment lists as
  locators only unless an enabled inspect tool returns an eligible claim.
- Current-run inspect claims override ViKnow expert model output on conflict.
- When claims conflict on the same fact and scope, prefer the primary evidence
  modality for that fact or state that evidence conflicts.

## Visual Grounding Delegation

- A knowledge-base answer slot about the count or location of visible targets
  must be verified by `visual_ground`, not by raw retrieval or a general VLM
  inspection claim alone.
- Before calling `visual_ground` for such a slot, load the `visual-ground`
  skill and use one selected evidence item or frame within the requested scope.
- A `visual_ground` result verifies only the selected image or frame; it does
  not establish an unobserved video-wide total or a different temporal state.
- Non-visual quantities must use evidence appropriate to their modality and
  must not be forced through `visual_ground`.

## Video Boundaries

- Copy `library_id`, `asset_id`, `evidence_id`, and `time_range` from the same
  selected evidence item whenever available.
- Do not invent, expand, shrink, or merge video time ranges for inspection.
- If broader coverage is required, select additional non-overlapping evidence
  items instead of modifying a time range.
- Do not inspect chunks by list position alone. Build a semantic candidate set
  for the answer slot.
- Do not hide limited coverage. Use scoped wording when only a subset of
  relevant segments was inspected.

## Uploaded Images

- Use `uploaded_image_search` only for user-uploaded workspace images, not for
  image Evidence returned by `knowledge_search`.
- If `uploaded_image_search.decision` is `mismatch` or `cannot_process_image`,
  explain the issue and ask for a relevant image or clarification instead of
  continuing knowledge search.
- Use `answer_from_image` as a final source only when the user asks solely for
  visible image content and not for knowledge-base, procedure, video, or
  indexed-material context.

## Citations

- Cite exact current-run `agent_citation` tokens returned by eligible tools.
- Do not cite failed results, zero-confidence claims, raw search text, internal
  evidence ids, file paths, timestamps, chunk ids, or source ranges.
- Never merge citation tokens. Write adjacent tokens separately.
- If a knowledge-base factual claim has no eligible citation, do not make that
  claim.

## Final Answer

- Answer the user's question once, from eligible evidence only.
- Preserve user intent. Do not silently broaden, narrow, or replace the
  question to fit available evidence.
- Cover each planned answer slot or explicitly state insufficiency for it.
- Do not append a second summary or restatement after the complete
  user-facing answer.
- Do not trade streaming/user-visible answer semantics for workaround-style
  deduplication unless the root cause and release condition are explicitly
  documented elsewhere.

## Optimization Boundary

- Automated optimization may improve wording, examples, trigger coverage,
  reference routing, and local clarity.
- Automated optimization may propose strategy changes, but must validate them
  against regression scenarios before they are applied.
- Automated optimization must not directly weaken safety, citation, evidence,
  permission, tool-ordering, or final-answer invariants.
- Any change introducing a fallback, alias, compatibility path, prompt
  strengthening, or silent degradation must explain why the root cause cannot
  be fixed at the source and when the workaround can be removed.
