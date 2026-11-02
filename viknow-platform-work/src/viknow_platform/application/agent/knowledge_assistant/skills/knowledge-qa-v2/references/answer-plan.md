# Answer Plan Operations

Read when changing goal definitions or binding a retrieval batch. Ordinary progress
and final self-review follow the root Skill; no separate read is required for every tool call.

A new run starts with an Answer Plan containing its current user input as
slot `answer`. The plan persists only within that run, including resume from
its checkpoint. For a clarification or follow-up, use conversation history to
interpret the current request and revise this run's goals; do not carry an older
run's plan into the current answer assessment. `answer_plan(action="start")`
restarts the current run's plan from its user input. This is an initial goal,
not evidence or a completed decomposition.
Keep it short for a direct question; split it as independent evidence needs
become apparent. The plan does not determine the question's difficulty in advance.

Give each slot a required evidence scope from its fact type and requested
source. A requested source is part of the fact. Evidence from one source may
support a separate answer slot, but it does not verify a slot scoped to what
another source shows. Use separate answer slots to compare observed behavior
and written requirements.

Use `query_refine` for suggested answer slots and retrieval queries when the current
goal needs independent facts, clearer scope, or a different exploration direction.
A precise direct question can keep the initial slot without calling this tool.

Pass the original user question unchanged as `query_refine.query`. Put helpful
discover output, conversation facts, formulas supplied by the user, or uploaded
image hints in the corresponding context fields; do not silently expand the
question and present the expansion as the original query.

`query_refine` returns suggestions, not the current plan. Decide which goals
to adopt or adjust, then record your decision with `answer_plan(action=update)`.
You can also define goals directly. When `uploaded_image_search`
returned `search_knowledge`, include its `visual_summary`, `search_hints`, and
useful `image_search_results` as context. Final coverage is judged against the
original question, including requirements the plan may have missed.

Respect each slot's role. An `evidence` slot is completed by independently
verified facts. A `collection` slot holds deduplicated `verified_items` and must
define `answer_unit`. For a calculation, create one evidence slot for each
independently retrieved input; do not create a slot for the calculated result.

Use `answer_plan(action=view)` to read current goals, evidence and remaining gaps.
Maintain it with `answer_plan(action=update, update=...)`, including when using
the initial slot without `query_refine`. Record plausible content-free locators as
`candidate_found`; after inspect, mark a slot `verified` only with claim refs
returned by inspect tools or listed as verified historical evidence in the plan
view. Historical claims retain their original source; reusing them is not a new
inspection. Mark a slot `not_found` only after its
planned retrieval and plausible candidates are exhausted. The runtime rejects
invented claim refs. To correct, split, merge, or remove a mistaken goal, supply
`update.slot_definitions` with the complete goal list to retain; omit it for
progress-only updates. Compare returned `goal_changes` with the original question and user
clarifications: revise your interpretation, not the user's requirements, to fit available evidence.

Use a goal's `context` for its current interpretation and assumptions. Record
supporting or conflicting observations, the remaining gap and a discriminating next check in
`slot_updates[].note`. Explain a changed interpretation in `update.rationale`; separate user
requirements from your assumptions. If the evidence answers a different question, retain the
unanswered requirement and clarify the mismatch. A supported answer needs no invented doubt.

Request another `query_refine` suggestion when useful, not as permission to
revise. Include the current plan, verified facts, unsuccessful directions, and
remaining questions in compact `conversation_context`.

The initial goal is not an obligation to obtain new inspect claims for an answer
already allowed by the historical-claim, expert-model, or uploaded-image policy.
In those paths, explain the evidence basis normally; do not fabricate a current-run
claim to close the goal. The existing answer/citation gate assesses the response.

## Retrieval Batch Bindings

One adopted set of query suggestions can guide an exploration batch. Choose
`knowledge_search` queries by the uncovered slots they serve; the original query
is a useful baseline, not a mandatory first call. To record an adopted batch, use
`answer_plan.update.retrieval_queries` with `library_id`, `query`, and current
`slot_ids` for each query. This replaces the recorded batch; omit it to retain
existing bindings. Bind only resolved library IDs; planning grants no access.
For searches already completed before adoption, record useful existing locators
with `slot_updates.candidate_locators`; do not rerun a search just to attach it.
Set `update.coverage_requirement` to `local` or `full_asset` when your scope
assessment changes. This records your judgment, not a mandatory
full-document traversal.
