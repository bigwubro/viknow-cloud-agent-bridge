# Evidence Candidate Audit

Read this reference when candidate selection is ambiguous, includes multiple
plausible results, spans modalities, or needs coverage expansion.

## Audit By Answer Slot

Keep candidate decisions attached to the `answer_slot` they may cover. For each
candidate, record one state and a short reason:

| State | Use when | Next action |
|---|---|---|
| `keep_for_inspect` | The candidate may directly verify the slot and contains the locator fields required by an inspect tool. | Call the matching inspect tool. |
| `reject_with_reason` | It is irrelevant, wrong-modality for the fact, a duplicate, or outside the requested context. | Exclude it from this slot. |
| `need_more_locator` | It may be relevant, but its evidence boundary or coverage is not reliable enough to inspect. | Use targeted retrieval or a modality-specific locator. |

Do not silently drop a plausible candidate. Do not select by search rank alone.

## Inspection Priority

When one or more candidates are `keep_for_inspect`:

1. Inspect the strongest non-duplicate candidates before another broad search.
2. Choose `document_inspect`, `video_inspect`, or `visual_ground` by the
   fact being verified, not by the asset container. Use `visual_ground` only
   for visible-target counts or locations.
3. Mark the slot `verified` when an eligible claim covers it.
4. If inspection fails, returns no eligible claim, or only partially covers the
   slot, inspect the next kept candidate.
5. Retrieve more only when no existing kept candidate can cover the remaining
   slot.

Prefer a small set that covers distinct plausible interpretations or event
instances. Deduplicate candidates that point to the same evidence boundary and
assert the same fact.

After one candidate produces an eligible claim, do not stop solely because the
slot now has evidence. Compare the unreviewed candidates with the original answer
target and the verified claim. A candidate remains plausible when its title,
section, period, category or stated value suggests that it may supply a different
part of the requested whole, a competing scope, or a contradiction. Reject it when
those same cues establish that it is off-scope or duplicates the verified fact.
Inspect the smallest set that can distinguish these possibilities.

For a direct-fact question, a claim that matches every binding element in the
original question closes the slot unless another candidate may contradict that
same claim. Do not retain candidates merely because they discuss the same company,
topic or outcome. A section-specific claim remains narrower than an unqualified
quantity; retain other non-overlapping sections that report the same metric for
the same period. A related model, page, attribute or code is not a substitute
for the requested one.

## Relationship Check Before Synthesis

For verified facts that could fill the same answer slot, state one relationship in
the final plan assessment:

| Relationship | Meaning | Answer treatment |
|---|---|---|
| repeated support | Same fact and scope appears more than once. | Use once; do not add it twice. |
| parts of one requested whole | Same metric, period and unit; non-overlapping scopes together match the requested scope. | Aggregate and show the inputs. |
| alternative scopes | Each value is supported, but the question or source does not establish which scope is intended. | Give conditional/scoped answers and record the uncertainty. |
| independent facts | Values answer different targets. | Keep separate; do not aggregate. |

Evidence that merely shares a topic is not enough to establish a relationship.
Do not use singular wording, search rank, section familiarity, or an assumed
convention to select one supported scope over another.

When the question requests an unqualified metric for one subject and period, and
the source expresses it only through non-overlapping components with matching
units, those components define the requested whole. Aggregate them and cite each
input. Treat component labels as alternative scopes only when the question or
source establishes that exactly one applies.

## Coverage Decisions

- `verified`: an eligible inspect claim covers the requested fact and scope.
- `uncovered`: candidates were absent, rejected, exhausted by inspection, or
  could not be converted into an inspectable boundary.
- Not ready: any plausible candidate still awaits inspection or rejection.

For an enumeration or total, report only a verified subset or lower bound until
the required catalog and candidate coverage is established; do not infer a
complete answer from a top-k result.

Modality conflicts and video coverage have separate entry conditions in the root
Skill's Conditional references section; this audit does not require another
reference read.
