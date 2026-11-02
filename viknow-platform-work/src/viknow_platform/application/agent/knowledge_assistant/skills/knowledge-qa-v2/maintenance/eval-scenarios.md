# Knowledge QA v2 Regression Scenarios

Use these scenarios to evaluate skill updates. They are behavior contracts, not
golden wording tests.

## Evidence Grounding

- A user asks a knowledge-base factual question. Search returns a highly
  relevant summary, but no inspect tool has run. Expected behavior: inspect the
  selected evidence before making factual claims.
- Search exposes an inspectable text, image, or video candidate for an
  uncovered answer slot. Expected behavior: classify it as `keep_for_inspect`
  and inspect it before issuing another broad search.
- Search returns several candidates with mixed relevance. Expected behavior:
  classify each candidate as `keep_for_inspect`, `reject_with_reason`, or
  `need_more_locator`; do not silently abandon a plausible candidate.
- A candidate remains `keep_for_inspect` when the agent is preparing to answer.
  Expected behavior: the answer slot is not evidence-ready, so inspect the
  candidate before answering or declaring insufficiency.
- Search returns indexed transcript text that appears to answer an audio
  question. No speech verification tool is enabled. Expected behavior: use the
  transcript as a locator only and state insufficiency if no eligible inspect
  claim can verify the speech fact.
- Search results conflict with an inspect claim. Expected behavior: rely on the
  eligible inspect claim for the final fact and expose remaining conflict when
  relevant.

## Uploaded Images

- The workspace contains an uploaded screenshot and the user asks what visible
  text it contains. Expected behavior: call `uploaded_image_search`, not
  `read_file` or direct model vision.
- The user asks how an uploaded photo relates to a procedure in the knowledge
  base. Expected behavior: call `knowledge_discover`, then
  `uploaded_image_search` with the selected library, then continue according to
  the image decision.
- `uploaded_image_search` returns `mismatch`. Expected behavior: explain the
  mismatch and ask for a relevant image or context; do not continue to
  `knowledge_search`.

## Tool Ordering

- The relevant library is not explicit. Expected behavior: call
  `knowledge_discover` before `knowledge_search`.
- An inspect call would need `evidence_id` and `time_range` from search.
  Expected behavior: wait for candidate evidence before calling the inspect
  tool.
- A planned `uploaded_image_search.library_id` depends on discovery. Expected
  behavior: do not issue both dependent calls in the same assistant turn.

## Video Inspection

- The user asks when an object state changes in a video. Search returns before,
  transition, and after candidates. Expected behavior: inspect a semantic set
  that can verify the transition, not only the top hit.
- The user asks about a whole video and search coverage is unclear. Expected
  behavior: use `list_video_segments` as a locator, then semantically select
  chunks for inspection.
- A candidate video range is too narrow for the question. Expected behavior:
  select additional evidence items; do not expand or invent the original
  `time_range`.

## Text And Citation

- `text_inspect` returns a claim with `quote_verified == false`. Expected
  behavior: discard the claim and state insufficiency if no other eligible claim
  covers the fact.
- The answer uses two eligible citations. Expected behavior: write exact tokens
  separately, such as `[1][2]`, never `[1-2]`.
- The available evidence only supports part of a multi-slot answer. Expected
  behavior: answer the covered slots and explicitly mark the uncovered slots as
  insufficient.

## Final Answer

- The user asks a narrow question, but retrieved evidence suggests a broader
  related topic. Expected behavior: preserve the original intent and avoid
  silently broadening the answer.
- The answer has already been completed. Expected behavior: stop immediately,
  without adding a second summary or restatement.
- Evidence is ambiguous or conflicting. Expected behavior: state ambiguity or
  conflict instead of resolving it with general knowledge.

## Skill Optimization

- Candidate selection is ambiguous, spans modalities, or needs coverage
  expansion. Expected behavior: read `references/evidence-candidate-audit.md`;
  for video-specific selection or coverage, also read
  `references/video-candidate-audit.md`.
- Search returns one clear, directly inspectable candidate. Expected behavior:
  inspect it directly; reading every modality reference is not required.
- A proposed update improves wording but touches no invariant. Expected
  behavior: allow it if scenarios remain stable.
- A proposed update changes evidence eligibility or citation requirements.
  Expected behavior: classify it as a boundary change requiring explicit human
  approval.
- A proposed update adds a fallback to hide repeated output or missing claims.
  Expected behavior: reject unless the root cause, blast radius, and removal
  condition are documented.
