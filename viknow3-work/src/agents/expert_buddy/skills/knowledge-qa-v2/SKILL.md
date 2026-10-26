---
name: knowledge-qa-v2
description: >
  answering questions about configured knowledge-base content, manuals,
  SOPs, product notes, training material, videos, documents, whether the
  knowledge base contains something, what documents/devices are available,
  or any request that might use knowledge, search, inspect, or evidence tools.
allowed-tools: knowledge_discover uploaded_image_search query_refine answer_plan knowledge_search list_video_segments list_doc_segments video_inspect_2_0 visual_ground document_inspect
metadata:
  version: 0.27.4
---

# ViKnow Knowledge QA v2

Use this skill for knowledge-base questions requiring search, inspection, or
evidence tools. Do not use it for general everyday questions.

## Non-Negotiables

- Search results are candidates only. Do not answer from raw `knowledge_search`
  text, indexed transcripts, indexed visible-text snippets, captions, or
  `list_video_segments`.
- Final factual claims must come from current-run inspect `claims` or valid
  historical verified claims.
- Discard failed tool results and claims with `confidence <= 0`. For
  `document_inspect`, only use claims returned by the tool.
- When the user has not fixed the scope, use `knowledge_discover`. Select a clear
  best match yourself; if multiple plausible sources would materially change the
  answer, state the ambiguity and ask the user to confirm the intended source.
- If evidence is partial, ambiguous, or conflicting, say so instead of filling
  gaps from general knowledge.
- A verified claim whose category, section or other scope is narrower than the
  original question does not complete that question. Before answering, inspect
  another returned candidate when its search text reports the same requested
  metric and period under a different explicit scope or value. This selects
  materially different candidates; it does not require inspecting every hit.
- When verified same-period, same-unit values are non-overlapping components
  that together cover an unqualified requested metric, answer with their sum and
  show each input. Otherwise report the values by scope.
- **Visible-object counts and locations require `visual_ground`.** For an
  answer slot about the count or location of targets visible in a selected image
  or video, read the `visual-ground` skill before calling that tool and base the
  final slot on its eligible claim. Do not force document, transcript, table,
  duration, or other non-visual quantities through `visual_ground`.

## Direct Answer Contract

Begin the final answer with `Answer:` or `答案：`, immediately followed by the
requested fact, final calculation result and unit, requested enumeration items,
or what cannot be determined. Add applicable citations there. Only afterward give
sources, arithmetic, details, or limitations. Never expose internal process.

## Tool Contract

- Keep the user's explicit source constraints; treat sources you select as
  hypotheses until their identity and relevance are established. A topic match
  does not establish that a library contains a named document. If results do
  not address the requested source, reassess your selection and use discovery
  to locate it. Changing a candidate source does not change the user's target.
- `list_video_segments` and `list_doc_segments` are bounded locator catalogs.
  For ordinary QA, call them with `mode=select` and one focused subquestion;
  their output is never answer evidence. Use `video_inspect_2_0` or `document_inspect`
  on selected locators before citing a fact.
- Once a selected library contains the named target or topic, keep retrieval in that
  library. Repeated search misses do not justify switching libraries; switch only
  when discovery shows the target is absent or the user explicitly requests
  cross-library coverage.
- `knowledge_search` returns candidates, not answer evidence. Inspect a plausible,
  inspectable candidate before broadening retrieval.
- Inspect calls must copy identity and boundary fields from one selected candidate.
  Only eligible inspect claims can support final factual claims.

## ViKnow Expert Model
Runtime may provide `ViKnow 专家大模型` before planning. Use it for terms, entities, relationships, professional background, and query planning. For conceptual or entity-relationship answers, it may support final claims; state that the answer is according to ViKnow expert model. Do not use it to replace retrieval and inspection for specific documents, videos, SOPs, parameter sources, timestamps, or indexed-content existence. Current-run inspect claims override ViKnow expert model on conflict.

## Reasoning Phases

These phases are not a fixed pipeline. Use only the phases needed for the
current question, but obey the tool ordering rules below.

### Phase 1: Grounding Setup

Decide whether the answer needs a knowledge base, a user-uploaded image, or
both.

Call `knowledge_discover` unless an explicit library is already supplied or the
question is clearly image-only. Pick the most relevant library.

Use `uploaded_image_search` only for a user-uploaded workspace image. If the
current workspace includes a user-uploaded image and the image may affect the
answer, call `uploaded_image_search` before `query_refine`, `knowledge_search`,
or any inspect tool. This includes direct image understanding intents: visible
text reading, counting visible items, object identification, scene description,
screenshots, charts, labels, interfaces, and photos. Do not answer directly
from model visual perception or `read_file` for these uploaded image questions.

If the uploaded image may affect a knowledge-base answer, call
`knowledge_discover` first, wait for its result, then call
`uploaded_image_search`. Pass the uploaded image `workspace_path`, the original
user `query`, and the selected `library_id` when a relevant library was found.
Omit `library_id` only when `knowledge_discover` found no relevant library, an
explicit library is unavailable, or the question is clearly image-only. Do not
guess image contents yourself.

Use the `uploaded_image_search.decision` value to choose the next phase:

- `answer_from_image`: answer from the uploaded image result. Only use
  `answer_from_image` as a final answer when the user asks solely for visible
  image content and not for knowledge-base, procedure, video, or indexed
  material context.
- `search_knowledge`: continue to query planning or retrieval.
- `mismatch`: explain that the uploaded image does not match the query or
  selected knowledge scope, then ask the user for a relevant image or more
  context. After `mismatch`, do not call `knowledge_search`.
- `cannot_process_image`: explain the image processing issue and ask for a
  better image or clarification.

### Phase 2: Query Planning

A new run starts with its current user input as slot `answer`, even for a follow-up.
Use conversation history to interpret references to earlier turns, then revise
this run's goals when needed. Do not treat an earlier run's plan as this run's
completion target. `answer_plan(action="start")` restarts the current run's plan.
Keep a direct question short; split goals as independent evidence needs appear.

Give each slot a required evidence scope from its fact type and requested
source. A requested source is part of the fact. Evidence from one source may
support a separate answer slot, but it does not verify a slot scoped to what
another source shows. Use separate answer slots to compare observed behavior
and written requirements.

`query_refine` returns suggestions, not the current plan. Pass the original
question unchanged and put additional context in context fields. Use it when
independent facts, ambiguity or a new direction need planning; precise questions
may keep the initial slot. Adopt or revise suggestions with `answer_plan(action=update)`.
You can revise goals without calling query_refine again.

Use view for current goals and evidence. Record candidates separately from verified
claims; not_found requires exhausted planned retrieval and plausible candidates.
Use goal context for interpretations, notes for supporting/conflicting observations,
gaps and next checks, and rationale for changes. Preserve the user's requirements
even when evidence answers a different question. A supported answer needs no invented doubt.

Before splitting, replacing or removing goals with `update.slot_definitions`, binding
a retrieval batch, or repairing plan payloads, read `references/answer-plan.md`.
The plan is investigation state, not evidence; historical, expert-model and
image-only answer paths do not require fabricated current-run claims to close a goal.

### Phase 3: Retrieval

Find candidate evidence.

For an initial focused search, prefer `top_k=5`; 3–5 is a useful starting range,
not a fixed limit. It caps the final evidence count, not internal retrieval.
If relevant results leave a coverage gap, a larger window such as 10 may help.
If results are off-target or repetitive, revise the subquery instead of merely
requesting more of the same. Top K results alone cannot establish exhaustive coverage.

Choose queries by uncovered slots; skip goals already verified.
An adopted batch may be recorded with answer_plan; read `references/answer-plan.md`
for bindings, progress updates and goal revision. Do not rerun a search merely to attach it.
Coverage records your current assessment, not a mandatory full-document traversal.

Within one library, do not repeat an already successful normalized query with an
equal or narrower result window. Reuse its candidates; increase `top_k` only when
the existing window cannot cover an unresolved slot. The same query may run in
another in-scope library. Skip planned queries whose slots are already verified.

Within a batch, collect candidates for the uncovered slots, then inspect or
reject plausible candidates. Do not conclude that evidence is absent while a
reasonable untried query still serves an uncovered slot. Review slot coverage
before deciding whether to refine the plan or stop retrieval.

After the first eligible claim, compare the remaining candidates with both the
original question and that claim. Keep a candidate when it may add another part
of the requested quantity, establish a different supported scope, contradict the
claim, or otherwise change the conclusion. Reject candidates that are clearly
off-scope or repeat the same fact at the same boundary. This comparison selects
the next useful inspection; it does not make every search hit mandatory.
For a direct-fact question, stop expanding when one claim directly covers the
requested subject, predicate, period and scope. A related fact about a different
predicate is not another answer. Continue across sections when the verified claim
is explicitly narrower than an unqualified quantity in the original question,
or when the question requests a list, comparison or total.

For each uncovered answer slot, classify relevant candidates as:

- `keep_for_inspect`: plausibly covers the slot and has an inspectable evidence
  boundary;
- `reject_with_reason`: does not cover the slot, lacks the evidence modality
  needed for the fact, or duplicates a stronger candidate;
- `need_more_locator`: may cover the slot but lacks a reliable inspectable
  boundary or sufficient coverage.

Inspect kept candidates before broadening beyond the current exploration batch.
When selection is ambiguous, spans modalities, or needs coverage expansion, read
`references/evidence-candidate-audit.md` before proceeding.

Use `list_video_segments` only when no kept search candidate can resolve the
video coverage gap, the user asks about a whole video, or temporal coverage
needs expansion. Start with action, object, or state terms from the uncovered
slot, refine the query before widening coverage, then call it with `mode=select`.
Do not request a whole catalog in ordinary QA. Read
`references/video-candidate-audit.md` before calling it. Use
`list_doc_segments(mode=select)` in the same way for document coverage gaps.
For an Answer Plan whose `coverage_requirement=full_asset`, read
`references/full-asset-coverage.md` before catalog traversal and collection updates.
Establish the catalog, review all bounded pages, and inspect retained/uncertain
candidates. Neither search hits nor a fully paged catalog proves a source-wide total.

### Phase 4: Evidence Verification

Inspect evidence by the information type needed for the fact, not by the
candidate container type. Use inspect tools only after candidate evidence has
been selected from `knowledge_search`, `list_video_segments`, or
`list_doc_segments`.

For video evidence, pass the selected `evidence_id` and an evidence-backed
`frame_time`. If a reliable frame image or user-provided time already answers
the requested temporal scope, use it directly. Otherwise, use `video_inspect_2_0`
to locate a suitable frame; its numeric claims locate the moment but do not
close the visual grounding slot. If no frame can represent the requested temporal
meaning with a distinguishable target, keep the slot unverified and state the
evidence limitation instead of inferring a count.

Pass a focused query that identifies and distinguishes the intended target;
the `visual-ground` skill defines that tool-specific request format.
Inspect `keep_for_inspect` candidates for each uncovered answer slot before
broadening retrieval. An eligible inspect claim is evidence for your assessment,
not an automatic declaration that a goal is complete.
Mark it `verified` only when its required evidence scope has an eligible inspect
claim; evidence outside that scope may inform retrieval but cannot close it.
After a failed, empty, or ineligible inspect result, the slot remains unverified.
Inspect the next kept candidate; keep the slot `pending` with a note when existing
candidates cannot cover it, then retrieve more if appropriate.

After a meaningful change in evidence or investigation direction, update the plan
with your conclusion and remaining gap. Routine tool calls need no separate plan update.
Before final synthesis, use `answer_plan(action=review)` to assess each current goal
against supporting and conflicting evidence: what interpretation was retained or
changed, what checks resolved the doubt, and what remains uncertain. Check the
original question for omitted or reinterpreted requirements, not just completion of the plan.
For multiple verified facts that could answer one requested quantity, record their
relationship in the assessment before choosing the answer: repeated support for the
same fact, non-overlapping parts of one requested whole, alternative scopes, or
independent facts. Treat them as parts of a total only when their metric, period and
unit match, their scopes do not overlap, and the original question asks for the
combined scope. Otherwise give the scoped values separately or record the remaining
interpretation in `uncertainties`; do not choose one by an unstated convention.
An unqualified metric asks for its overall value within the stated subject and
period. If the source reports that metric only as non-overlapping components whose
scopes together cover that request, use their combined value as the direct answer
and show the inputs. Do not turn source section labels into alternative user
interpretations unless the wording or evidence makes those scopes mutually
exclusive.
For each unresolved gap, state its effect on the conclusion: partial findings do not establish
a total or absence. Review takes `items` and `original_question_check`,
required `uncertainties`, and optional `limitations`; use an empty uncertainty list
only after checking that no materially different supported interpretation remains.
In the same review, set `conclusion=answer_supported` only when evidence supports a substantive
answer to the requested fact, including a supported partial answer with clear limits. Set
`conclusion=requested_fact_unanswerable` when the requested fact cannot be determined; related,
conditional or possible answers do not make it answered. Record the evidence basis in the required
`conclusion_reason`.
The runtime owns version tracking, so no revision is needed.
Use `view` when you need current evidence. A recorded review is your assessment, not an independent correctness verdict;
the final answer must use the evidence citations that support its actual statements.
Do not calculate merely because
one input was found; every required input slot must be verified or explicitly
not found.

For collections, define answer_unit and record distinct inspect-supported
verified_items, not chunks or citations. Read `references/full-asset-coverage.md`
before recording collection identities, grouped quantities or full-source completion.

For numeric calculations, bind every input to a verified value and supporting claim.
Once every input is verified, read `references/sandbox-computation-rules.md` and
use DeepAgents `execute` for the calculation.
Do not calculate from values that exist only in search or catalog output.
Count deduplicated answer units, not chunks, hits, citations or inspect calls.

### Visual Grounding And Time Scope

- Match the evidence scope to the requested fact. A frame-local count or location
  requires a current-run `visual_ground` claim on a reliable frame time. When a
  reliable frame is already supplied by evidence or the user, do not require an
  extra video inspection merely to recover that same timestamp.
- A source-wide count concerns events or items across the selected video or
  document. One frame cannot prove a source-wide total; establish catalog
  coverage, inspect retained candidates, and deduplicate answer units.
- For duration, verify the semantic start and end boundaries with eligible
  inspect claims. Catalog summaries are locator hints, not proof of the duration.
- Classify counting requests as frame-local objects, repeated actions, global text
  occurrences, duration or parameter, or global categories. A single frame cannot
  establish an across-time total. For cumulative occurrences, audit the bounded
  catalog, inspect plausible occurrences, and expand coverage before answering zero.
- For continuing actions, use a verified final-state frame for visible-object
  counting. Confirm that the frame, object alias, and requested temporal scope
  match before answering.

### Phase 5: Answer

Use verified claims, valid historical verified claims, or `uploaded_image_search`
with `answer_from_image`. Cite each knowledge-base factual claim with its exact
returned token. Without eligible evidence, state insufficiency.
Each evidence-dependent slot must be verified or have a concrete uncovered
reason; plausible candidates awaiting inspection or rejection mean it is not ready.
Answers only stating insufficiency, library metadata, or no knowledge-base facts
need no citation.

## Tool Ordering Rules

These are partial-order constraints, not a mandatory full sequence.

- For a relevant user upload, use `uploaded_image_search`, not `read_file`,
  before knowledge retrieval or inspection.
- Call `document_inspect` only after `knowledge_search` has returned selected document
  evidence. It reviews the full source page; do not use it for user-uploaded workspace images.
- For selected video evidence, `visual_ground.frame_time` must come from a
  reliable frame image, `video_inspect_2_0`, or user-provided timing; never invent
  a timestamp without a visual basis.

## Reference Loading

- `references/answer-plan.md`: goal replacement, retrieval bindings and plan payload details.
- `references/full-asset-coverage.md`: full-source catalog traversal and collection bookkeeping.

Read references only when the matching condition applies:

- `references/inspect-results.md`: inspect result eligibility.
- `references/evidence-candidate-audit.md`: ambiguous, multi-candidate,
  cross-modal, or coverage-expansion candidate selection.
- `references/generation-guidelines.md`: specialized comparison, workflow or other output templates.
- `references/evidence-modality-policy.md`: multimodal evidence conflicts.
- `references/video-candidate-audit.md`: video-specific chunk and temporal
  coverage selection.
- `references/claim-reconciliation.md`: same-fact claim merging/conflicts.
- `references/citation-rules.md`: citation eligibility or formatting details beyond the rules below.
- `references/sandbox-computation-rules.md`: exact calculation with `execute`.

## Tool Capability Boundaries

- `uploaded_image_search` analyzes one user-uploaded image before text query
  refinement. Use it only for workspace uploads, not for image Evidence returned
  by `knowledge_search`.
- `video_inspect_2_0` extracts traceable claims from one selected video
  evidence range. It may adapt frame resolution, crop a relevant region, or
  increase sampling around an uncertain moment. It does not transcribe speech,
  verify exact audio wording, search other ranges, or combine multiple clips.
- visual_ground verifies object counts and spatial grounding from images or
  sampled video frames. For video, it samples the selected evidence time range
  and returns inspect-style claims. It does not search for candidate evidence and
  does not replace `video_inspect_2_0`.
- For speech, dialogue, reassurance, reminders, announcements, or audio
  instructions, use indexed transcript snippets only as locator hints. If no
  enabled inspect tool can verify the fact as a claim, state that evidence is
  insufficient.
- For visible text in media, use visual text snippets only as locator hints.
  Verify only what an enabled inspect tool can read as a claim; otherwise state
  that evidence is insufficient.
- For document/SOP text, tables, figures, and charts, call `document_inspect` on
  evidence copied from `knowledge_search` or a segment locator returned by
  `list_doc_segments(mode=select)`. It resolves the page context before inspection.

## Video Evidence Calling Policy

Before video inspection, read `references/video-candidate-audit.md` for copied locator
boundaries, sampling defaults, candidate budgets and justified repeat calls.
The main Agent reconciles claims across ranges; do not invent locator boundaries.

## Primary Evidence Modality

Choose the primary evidence modality by the fact being verified:

- Visible action, count, posture, object state, or scene content -> video.
- Visible written text, labels, signs, panels, or screen content -> image/video
  only when the enabled inspect tool can read it reliably.
- Document/SOP rules, parameters, or written procedure text -> text.

If modalities conflict, prefer the primary modality for that fact. Use other
modalities as context or locator evidence only. When unsure, see
`references/evidence-modality-policy.md`.

## Claim Reconciliation

Before treating claims as conflicting, decide whether they assert the same fact
under the same modality scope. A visual absence claim limits what was visible in
frame; it does not negate speech evidence from the same time range. If claims
answer different scopes, combine them with explicit boundaries. If they conflict
on the same fact and scope, prefer the primary evidence modality for that fact or
state that evidence conflicts. See `references/claim-reconciliation.md`.

## Historical Claims

Historical verified claims may be used only to restate or summarize the same
fact. Copy their stable `citation_token` exactly. If the user asks about a new
state, count, later time point, action result, or uncovered property, call the
corresponding inspect tool again.

## Final Answer Generation

Use the final plan review to check the original question's scope, omissions,
additional matching items and conflicts. Revise the plan or state unresolved limits.

- Keep original terminology, part numbers, protocol names, document titles and
  standard abbreviations.
- Put the final user-facing answer in a message with no tool calls; finish any
  housekeeping tools in an earlier turn.
- Answer from verified claims; express unresolved scope in the conclusion itself,
  not as a disclaimer after an unsupported total. Partial answers or clarification are valid.
- Preserve source units, currency and scale unless the user requests conversion.
  If calculation requires unit normalization, explain the conversion and label the result's unit.
- Answer only the output requested by the user. Do not append document-wide
  background, related-item lists, or other verified facts that do not answer an
  explicit answer slot.
- Do not write tool display_label, timestamps, segment IDs, or source ranges in
  answer text. The citation renderer resolves those details from citation tokens.
- Do not present raw tool metadata, chunk ids, file paths, or internal evidence
  ids as user-facing source labels.
- Do not narrate searches, inspections, plan updates, or tool failures unless a
  concrete evidence limitation changes the answer; then state only that
  user-relevant limitation.
- Write the complete user-facing answer once, then stop; do not append a second
  summary or restatement of the same answer.
- If you need to correct the answer, rewrite that same answer in place; do not
  append a separate summary after a finished answer.

## Citation Rules

- Use exact current-run `agent_citation` tokens returned by tools: `[1]`, `[2]`.
- Never merge tokens. Write `[3][4][5]`, not `[3-5]`.
- For more examples, see `references/citation-rules.md`.
