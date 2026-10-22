# Full-Asset Coverage And Collections

Read for full-source traversal or collection bookkeeping. Collection identity rules
also apply to local collections; do not infer full-asset scope from a count alone.

## Catalog Coverage

For an Answer Plan whose `coverage_requirement=full_asset`, first establish the
asset catalog with
`mode=map`, then consume bounded `mode=page` results in cursor order until
`next_cursor` is empty. Immediately after each page, use
`answer_plan(action=update)` with `update.catalog_reviews` to classify every returned `segment_id`
exactly once as `candidate`, `uncertain`, or `excluded`, and bind the decision
to the affected answer slots. Keep a segment uncertain when its bounded summary
cannot safely exclude it. Give excluded segments a short reason grounded in the
current page summary. Inspect every candidate and uncertain segment; search hits
or a fully paged catalog alone cannot prove a source-wide total.

## Verified Collection Items

When a slot declares `answer_unit`, record every distinct, current-run
inspect-supported item in `verified_items`. Use an identity based on the fact
itself (for example one chart, event, clause, or course), not the containing
chunk or segment. A single inspected chunk may contribute multiple items, and
the same item observed in multiple claims must keep one normalized identity.
If one inspect claim verifies that a grouped candidate contains multiple
answer units but does not provide stable labels for every child, record one
group identity with `quantity` equal to the verified child count. Do not also
record those children separately. `quantity` must come from an eligible
inspect claim, never from a search or catalog summary.
Do not record an item from search/catalog summaries alone. For full-asset
collection, mark the slot verified only after source coverage is complete and
every retained candidate has been inspected or explicitly excluded.
