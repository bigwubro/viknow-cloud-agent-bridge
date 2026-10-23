# Knowledge QA v2 Optimization Policy

Use this policy before proposing or applying automated updates to
`knowledge-qa-v2`.

## Change Classes

- Expression changes: wording, examples, headings, trigger phrases, and
  reference navigation. These may be automated when they preserve invariants.
- Strategy changes: retrieval planning, candidate selection, inspection
  heuristics, synthesis structure, and reference loading. These require
  regression evaluation before merging.
- Boundary changes: evidence eligibility, citation requirements, tool ordering,
  permissions, user-uploaded image handling, and final-answer semantics. These
  require explicit human approval and must not be auto-merged.

## Required Update Record

Every proposed update must include:

- Source case: the concrete failure, confusion, eval result, or user feedback
  that motivated the change.
- Root cause: why the current skill allowed the bad behavior.
- Change class: expression, strategy, or boundary.
- Minimal diff: the smallest instruction change expected to fix the issue.
- Impact analysis: which existing flows might change.
- Regression result: which scenarios were checked and what changed.

Reject broad "make it better" edits with no source case.

## Optimization Flow

1. Read `invariants.md`.
2. Identify the failure or improvement target.
3. Decide whether the issue is caused by missing instruction, unclear routing,
   model misread, tool limitation, or upstream runtime behavior.
4. Prefer source fixes over fallback instructions. Add workaround instructions
   only when the source cannot be fixed now.
5. Apply the smallest eligible change for the change class.
6. Run or reason through the relevant scenarios in `eval-scenarios.md`.
7. Do not merge if any invariant regression appears.

## Tool And Schema Changes

When tools are added, removed, renamed, or their result schema changes, do not
regenerate all boundaries by default. Perform a boundary impact review:

- Which facts can the tool verify directly?
- Which outputs are only locators or candidates?
- What tool ordering dependencies does it introduce?
- What makes a result eligible or ineligible for final claims?
- How should citations be produced or preserved?
- Does it affect uploaded-image, video, text, audio, or expert-model behavior?
- Which regression scenarios need to be added or changed?

Update only the affected invariant sections, reference routing, and regression
scenarios. Full regeneration is reserved for a major redesign of the QA
contract.

## Rejection Rules

Reject or escalate any update that:

- Allows final factual claims from candidate evidence.
- Removes required inspection before knowledge-base claims.
- Weakens citation requirements for fluency.
- Lets uploaded images bypass `uploaded_image_search`.
- Lets video inspection invent or reshape time ranges.
- Silently converts uncertainty, conflict, or missing evidence into a confident
  answer.
- Adds compatibility or fallback behavior without root-cause explanation and
  removal criteria.
