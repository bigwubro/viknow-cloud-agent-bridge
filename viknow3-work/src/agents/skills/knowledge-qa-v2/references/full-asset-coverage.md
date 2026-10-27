# Full-Asset Coverage And Collections

Read for full-source traversal and deduplicated collections. A count alone does not
establish full-asset coverage. The text plan records investigation intentions and gaps,
not machine-certified coverage or evidence.

## Catalog Coverage

When the original question needs full-source coverage, establish the asset catalog
with mode=map, then consume bounded mode=page results until next_cursor is empty.
Record candidate/uncertain/excluded decisions with short reasons in a run-sandbox
working file when the list is long. Retain uncertain segments unless the summary
safely excludes them. Inspect candidate and uncertain segments. Search hits or a
fully paged catalog alone do not establish a whole-source total.
Update answer_plan with action=update and content containing a short complete plan:
checked scope, gaps, next step and optional working-file path. Do not pass catalog_reviews,
slot_updates, verified_items or other nested update fields.

## Distinct Supported Items

Record inspected items in a run-sandbox working file, using an identity based on the
fact (one chart, event, clause, course), not its containing chunk or inspect call.
Keep the item label, supporting short evidence key, source location and quantity
when the evidence explicitly establishes a grouped count. Do not count both a group
and its children. Normalize repeated appearances to one identity and use execute
for deduplication and arithmetic. Search/catalog summaries are candidates, not facts.

If traversal, inspection or deduplication remains incomplete, state the verified subset
or lower bound and uncovered scope. Do not present top-k hits or a partial list as a
whole-source answer. In answer_plan action=review, content should explain completed
work, remaining gaps and the chosen limited answer. It does not certify correctness.
