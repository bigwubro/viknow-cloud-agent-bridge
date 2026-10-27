---
name: knowledge-qa-v2
description: >
  answering questions about configured knowledge-base content, manuals,
  SOPs, product notes, training material, videos, documents, whether the
  knowledge base contains something, what documents/devices are available,
  or any request that might use knowledge, search, inspect, or evidence tools.
allowed-tools: knowledge_discover uploaded_image_search answer_plan knowledge_search list_video_segments list_doc_segments video_inspect visual_ground document_inspect
metadata:
  version: 0.31.0-final-check-tool
---

# ViKnow Knowledge QA v2

Use this skill for knowledge-base questions, not general everyday questions.
The original question—not the available search hits—defines the answer target.

## Evidence and answer invariants

- Search results, catalogs, transcripts, indexed visible text and captions
  locate candidates; they are not answer evidence. Ground factual claims in
  eligible current-run inspect claims or valid historical verified claims.
  Discard failed results and claims with `confidence <= 0`. For documents use
  only `document_inspect` claims.
  Library-inventory answers are an exception only for returned authorized
  library metadata: names and provided summaries, not document-body facts.
- **Pre-answer verify gate.** Before final output, decompose the question into
  its binding elements: requested subject/entity/model/dataset, premise,
  predicate, time/period, unit, attribute, and page/scope. Check every element
  against eligible evidence only.
  - If all binding elements are covered, prepare to answer the requested fact
    directly, binding each fact to the actual returned citation alias.
  - If any binding element is not covered, retain that gap and explain what
    cannot be determined. Do not present a substitute, adjacent, partial, or
    inferred fact as the requested answer. No self-review form is required.
- Do not repair a missing element by mapping to a similar, adjacent, or
  analogous term: a different model, model family, or version; a differently
  named code; a similar color, object, or word; a co-located item; or an
  adjacent page/value. Do not accept a subject or premise link supported only
  by inference or plausibility rather than a direct eligible claim.
- Do not convert a substitute fact, numeric zero, or out-of-range result into
  the requested answer, and do not silently change a false premise. A partial
  or substitute answer must never be marked `answer_supported` for the
  original fact.
- Refusal requires a genuine, exhausted retrieval attempt: discovery, search,
  and catalog coverage for the named subject, not a single limited query.
- Match each claim to the requested subject, source, predicate, time, unit,
  attribute and scope. Do not silently change a false premise, turn missing
  evidence into numeric zero, or present a related fact as the requested answer.
- For multi-part questions, answer supported parts and explicitly limit
  unsupported parts. A partial answer must not masquerade as a complete one.
- A count, list, comparison, maximum or total requires whole-scope enumeration
  with bounded catalog coverage; top-k hits, inspected chunks, a catalog sample
  or one frame do not establish a source-wide answer. Count distinct answer
  units, not hits or citations. If full coverage is unproved, give only the
  verified subset or lower bound and state what remains unverified; never
  present a partial subset as complete.
- For complex visual evidence such as diagrams, charts or mixed-color figures,
  extract distinct relevant groups separately (for example by color, shape,
  label or page) when one inspection cannot cover the requested scope. Keep
  each group's inspected evidence and do not infer a whole from one figure.
- When the question combines conditions, list the evidence-backed items for
  each condition first, then explicitly compute the requested intersection
  or union. Deduplicate answer units before counting or listing them.
- Treat genuinely conflicting same-scope claims as unresolved. Combine
  non-overlapping components only when metric, period and unit match, their
  scopes jointly cover the requested whole, and the inputs are cited.
- Final answers are generated and submitted only through the no-argument
  `final_check()` tool. Finish other tool calls first and call it alone;
  do not directly send a final prose answer, answer payload, or self-review.
  The runtime selects the final structured format and renders Markdown; the
  Agent does not supply that payload. Fact-bearing `text` stays clean, with exact
  returned aliases such as `[C1]` / `[H1]` in its own `claim_refs`. Comparison
  cells and sequence steps bind separately; program-owned labels, step numbers
  and unknown cells are structure.
  Never write source labels into `text` or collect all references at the end.
  In generic unit output that uses citations, every nonblank text unit must bind evidence.
  Merge structural headings, transitions and limitations into their related
  supported unit, preserving wording and line breaks. Do not merge independent
  facts or the whole answer merely to collect citations at the end.
  Only the original question's requested standalone Final answer letter line
  may remain unbound. An entirely uncited response can explain limitations;
  the runtime validates its syntax, not whether its conclusion is factually correct.

{{qa_final_answer_format_examples}}

## Start with the user's scope

Choose whether the question asks for a library inventory or needs evidence
from a knowledge base, a user-uploaded image, or both. For library inventories,
follow Catalog enumeration below. For evidence questions, call
`knowledge_discover` with a non-empty query unless a library is explicit or the
question is clearly image-only. Select a clear best library; ask which one if
plausible sources would materially change the answer. A topic match does not prove that
a named document exists in that library. If the selected source does not cover
the user's named target, use discovery again rather than changing the target.

### Catalog enumeration

An omitted, empty or whitespace-only `query` in `knowledge_discover` is only
for explicit library-inventory requests such as "你有哪些知识库？" or
"List the available knowledge libraries." Answer from the authorized library
names and provided summaries; do not search or inspect document bodies merely
to enumerate libraries. Omit `top_k` for a complete list. If the user requests
a limited list, `top_k` may limit it, but do not present that subset as complete.

Never use an empty query, with or without `top_k`, to recover from empty
discoveries in ordinary knowledge QA. Do not enumerate the catalog and then
read all summaries to select libraries yourself: query-based discovery must
perform that screening. This applies to short topic questions, formulas,
identifiers, and questions spanning multiple libraries, not only named sources.
For a question asking both for a library list and a substantive answer, keep
the inventory and evidence-retrieval goals separate; the inventory listing
does not select libraries for the evidence question.

### Library routing

For each evidence-retrieval answer slot, follow this sequence. Directory
discovery locates libraries; it does not search body evidence.

Query-based discovery filters catalog candidates by topic relevance. A returned
`score` is the reranker's relevance score, not a rank-derived value or a
calibrated probability. Generic words shared by unrelated summaries are not
enough to select a library. Inventory listings have no relevance score.

1. Call `knowledge_discover` with a focused, non-empty query for the original
   subject.
   Preserve named documents, identifiers, equipment models and process
   constraints in query revisions. Do not replace a named-target query with
   a generic topic or an empty catalog listing.
2. If `libraries` is nonempty, copy a returned `id` into
   `knowledge_search(library_id=..., scope="library", query=...)` and search
   for concrete evidence. An empty single-library search is not permission
   to cross libraries. A slot that ever had a discovery hit cannot use
   cross-library search during this run.
3. If `libraries=[]`, revise the non-empty query and retry `knowledge_discover`
   for the same `slot_id` while its existing run-wide successful discovery
   budget remains. Do not call `knowledge_search` between these empty
   discoveries. Follow the harness's settled counts and next-step guidance,
   not an assumed retry count; duplicate calls, errors, rejected calls and
   in-flight reservations do not prove successful budget exhaustion.
4. Only after that budget is exhausted, the slot never had a discovery hit,
   and all discoveries have settled, call
   `knowledge_search(scope="cross_library", slot_id=..., query=...,
   bm25_keywords=[...])`. Do not pass `library_id`. Never omit both
   `library_id` and `scope` as an implicit request to search all libraries.
   `query` is the semantic evidence query; `bm25_keywords` preserves original
   query terms, formulas and identifiers, not expansions or guessed answers.

The run request's library whitelist applies throughout; do not supply your
own authorization range. Cross-library search is not an escape from an
unsuitable discovered library or a user request to bypass routing. For a
multi-library question, discover each slot and use explicit single-library
searches wherever discovery hits.

Tool errors and gate rejections are not empty evidence results. If another
hard budget, permission boundary or upstream failure prevents completion,
state that limitation rather than claiming the named source does not exist.
Otherwise retrieve evidence before concluding that it is insufficient, then
inspect plausible candidates under the evidence rules below.

If a user-uploaded workspace image may affect the answer, call
`uploaded_image_search` before knowledge search or inspect. For combined
image-and-knowledge questions, discover the library first, then pass the image
path, original query and selected library to image search. Do not answer an
upload from `read_file` or unverified model vision. Respect its decision:
`answer_from_image` can close an image-only question; `search_knowledge`
continues retrieval; `mismatch` and `cannot_process_image` require an
explanation or clarification, not speculative retrieval. Do not use this tool
for images returned by knowledge search.

ViKnow expert model context may help terminology, entities and query planning.
It may support conceptual claims if identified as such, but never replaces
inspection for a specific document, video, SOP, parameter, timestamp or
indexed-content existence. Current-run inspect claims override it on conflict.

## Plan only the evidence still needed

Select an answer template explicitly with the existing `answer_plan` tool's
`start` or `update`, prioritizing the user's explicit format request:

- `template_id="generic"`: ordinary answers that do not need the formats below.
- `template_id="comparison"`: object differences or before/after state or quantity changes.
- `template_id="sequence"`: reconstruct observed event order or an observed process.

Choose from the underlying task even for multiple-choice questions. Omitting the
field retains the selection; change it only when the task or organization changes.
Successful selection directly returns the canonical core template from
`references/answer-templates.md`; reading that file alone never activates one.
Keep `content` and `findings` under their existing replacement rules.
For comparison, preserve both sides, shared attributes and scope gaps when closing;
an option is a conclusion, not one comparison side.
For sequence, record actual observations, their order and uninspected intervals in
the plan and closing note. Restore what happened rather than turn it into normative
instructions or advice. Options are conclusions, not steps. The program numbers
steps; step text has no handwritten number or source label, with each step bound
to its actual returned evidence in `claim_refs`. Complete examples live only in
the canonical reference, automatically attached on selection.


Start this run from the current user question, even for a follow-up. Use history
to resolve references, not to inherit an earlier plan's completion target.
Keep direct questions as one investigation target; split only genuinely independent facts.
Retain each target's requested source and evidence scope in the plan. Do not close a
source-specific target with evidence from another source.

Use `answer_plan(action=update)` only when key findings, ambiguity or a changed
investigation materially change the plan; batch related inspections before updating,
rather than rewriting it after every tool result. Record candidates separately from verified claims;
the plan is investigation state, not evidence or publication authorization.
After no-progress searches, identify a different retrieval direction or record
the gap; do not repeat the same query or plan update. Read
`references/answer-plan.md` when needing complete text-plan examples. It is the single
source of complete call examples. On a deterministic plan payload error, Harness
attaches the applicable section to the original tool error. Use that content
without an extra read; if loading is unavailable and a file_path is supplied,
read that absolute path with read_file before correcting the call. Plan evidence
bindings use the same returned `[C1]` / `[H1]` keys as final generation; never copy internal IDs.

For comparisons, keep both subjects, candidate shared dimensions and the current
investigation arrangement in `content`; record meaningful changes without a tool-call diary.
Use individual `findings` for each side's actual observations and returned evidence keys.
The closing `review` preserves the chosen dimensions, unverified sides and scope gaps;
it is not a correctness assessment. Final organization uses this plan, while facts
still require the eligible claim catalog. Read the comparison section of
`references/answer-plan.md` when needing complete positive and negative examples.

## Retrieve, inspect, then stop

Search for uncovered slots with focused queries; `top_k=5` is a starting
point, not an exhaustive range. Reuse successful same-library queries and
their candidates. Increase the window only for a real coverage gap; refine
off-target queries rather than asking repeatedly for more of the same.
Inspect a plausible candidate before broadening. Once discovery has returned
a library for a slot, keep evidence search in actual discovered libraries.
Further discovery can identify another relevant library, but cannot unlock
cross-library search for a slot that already had a discovery hit.

After the first eligible claim, compare remaining candidates to both the
question and the claim. Inspect those that may supply another requested part,
a different scope, a contradiction, or missing coverage; reject duplicates and
off-scope hits. For a direct fact, stop expanding when one claim covers the
requested subject, predicate, period and scope and no material contradictory
candidate remains. Do not inspect every hit by default.

Use `list_doc_segments(mode=select)` or `list_video_segments(mode=select)`
when search lacks a reliable locator or whole-source coverage is needed.
Their catalogs are bounded locators, not answer evidence. Read
`references/evidence-candidate-audit.md` when candidate selection is
ambiguous or cross-modal; read `references/video-candidate-audit.md` before
video catalog or inspection; read `references/full-asset-coverage.md` before
full-source enumeration, count, comparison or collection bookkeeping.

Inspect selected evidence with the tool suited to the fact:

- Use `document_inspect` for document text, tables, figures and charts; use
  candidate identity and boundaries returned by search or document catalog.
- Use `video_inspect` for selected video ranges. Assess whether its claims
  verify the requested fact and scope; it can also locate a reliable frame.
- Choose `visual_ground` when the fact needs visible-object counting or spatial
  localization in one image/frame. Decide from the fact and available evidence,
  not words such as "多少" or "how many". First read the
  `visual-ground` skill; pass a reliable `frame_time` from the user or video
  evidence, never an invented timestamp. If the user gives a reliable
  `frame_time`, skip video inspection solely to locate that frame. Do not force
  textual, transcript, tabular, mass or duration quantities through visual grounding.
  For example, "在化学实验第3步的时候用了多少克Mg" asks for a mass, not
  a count of visible objects; choose the inspect tool that can verify that mass.
- Speech/transcript snippets and indexed visible text are locator hints. If
  no enabled inspect tool can verify the exact fact, say so.

An ineligible, empty or failed inspection leaves its slot open. Inspect the
next useful candidate or record the gap. If the question spans a whole source,
establish bounded catalog coverage and inspect retained or uncertain items
before closing the answer. A single frame is not proof of an across-time
total; for durations verify start and end boundaries. For exact calculations,
verify every input, then follow `references/sandbox-computation-rules.md`.

After meaningful evidence changes, update the concise plan. Compare the
original question with verified evidence, remaining gaps and contradictions.
No structured review/assessment form is required. When ready to answer, including an
honest partial answer, refusal or clarification, call `final_check()` alone
with no arguments. Maintain answer_plan with action=update and a concise content string
containing the complete current plan. Do not pass update objects, slot_updates,
slot_definitions or status enums. Before final submission, use action=review with
content describing completed work, remaining gaps and why investigation can end.
Keep source labels out of content and findings[].text. Record each evidence-backed
conclusion in findings with its own text and actual claim_refs. update replaces the
whole findings list (omitted means empty); review omits findings to retain it.
Use references/answer-plan.md for the single maintained set of complete examples.
This is a closing note, not an answer correctness assessment. Changed plan text
needs a new closing note; unchanged text does not require repeated review on retries.
For unavailable sources, record the obstacle and intended limitation in plain text;
never invent evidence or mark the source verified. Calculations require code execution.

If the tool explicitly requests a final explanation after repair is exhausted,
write that explanation once as plain text; do not call tools or final_check again.
For example, wrong: call final_check again or paste malformed JSON/internal IDs;
right: explain what could not be completed and any conclusions you can confirm.
The last failed draft is recovery material, not evidence.

## Answer once and within scope

Before writing, rerun the pre-answer verify gate: every binding element must
match eligible evidence. If one or more do not, lead with what cannot be
determined and state the failing element. Do not map it to a similar term,
substitute a nearby fact, or present it as an established answer.

For generic answers, begin with `Answer:` or `答案：`, immediately followed by the requested fact,
calculated result and unit, requested items, or what cannot be determined,
with applicable citations. Then give only needed sources, arithmetic or
limitations. Preserve the user's terms and source units. Do not add unrelated
background, a second answer, or internal tool/process narration. Do not expose
chunk IDs, file paths, tool display labels or raw evidence metadata.

Give a verified alternative fact only as clearly labeled supplementary context
after stating that the requested premise or scope failed; never put it in the
answer slot for the requested fact. For multi-part answers, distinguish the
supported and unsupported parts. For enumerations, show the qualifying items
and their cited basis; do not present inspected subsets as complete.

Use valid historical verified claims only to restate the same fact, copying
their stable citation tokens. A new state, count, later time or uncovered
property requires fresh inspection. Finish housekeeping tools before calling
`final_check()`. Do not send a separate user-facing answer after it succeeds.

## Conditional references

- `references/answer-plan.md`: text-plan actions and complete examples.
- `references/answer-templates.md`: canonical format templates, returned on explicit selection.
- `references/inspect-results.md`: inspect eligibility details.
- `references/evidence-candidate-audit.md`: ambiguous candidate selection.
- `references/video-candidate-audit.md`: video ranges and temporal coverage.
- `references/full-asset-coverage.md`: full-source catalog and collections.
- `references/evidence-modality-policy.md`: primary modality by fact type.
- `references/claim-reconciliation.md`: same-scope conflicts.
- `references/citation-rules.md`: citation formatting details.
- `references/sandbox-computation-rules.md`: exact calculation.
- `references/generation-guidelines.md`: specialized output forms.
