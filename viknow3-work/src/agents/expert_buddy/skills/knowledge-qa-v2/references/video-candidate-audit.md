# Video Candidate Audit

Use this reference when selecting video
chunks for `video_inspect`, expanding temporal coverage, or deciding whether to
call `list_video_segments`.

## Build The Candidate Set

From `knowledge_search` results, classify VIDEO candidates semantically. Mark a
chunk `keep_for_inspect` only when it may directly verify the answer slot:

- Does it contain the observable event/action in the answer slot?
- Does it contain the target object/person/location?
- Does it satisfy the temporal or state condition?
- Does it match the current `answer_plan` goal and context?
- Is it a distinct plausible event instance from already kept chunks?

Do not choose by result order alone. Deduplicate candidates from the same asset
when their time ranges overlap and their descriptions refer to the same event.
Copy `library_id`, `asset_id`, `evidence_id`, and `time_range` from that same
candidate when inspecting it.

## First Audit Round

- Inspect every kept VIDEO candidate, up to 5 VIDEO chunks per answer slot.
- If search yields only one semantically valid candidate, one `video_inspect` is
  acceptable.
- Inspect kept search candidates before calling `list_video_segments` or
  issuing another broad search.
- If only first-round search candidates were inspected, do not claim whole-video
  coverage. Scope the answer to the inspected relevant segment.

## Locator Audit

Use `list_video_segments` when:

- search results do not provide an inspectable boundary for a plausible event;
- the user asks why only one video/chunk was checked;
- wording may refer to repeated event instances in the same video;
- the user asks for all/whole/global video understanding;
- verified claims conflict or leave the answer slot uncovered.

Do not call `list_video_segments` merely because the candidate is a video. A
clear search result with a valid evidence boundary should go directly to
`video_inspect`.

After `list_video_segments`, select new candidates semantically:

- 5 or fewer new VIDEO chunks need `video_inspect`: inspect directly.
- More than 5 need `video_inspect`: warn the user that this may take significant
  time and ask whether to proceed or narrow the scope.

## Example

If search finds only "open elevator door at 32-62s", inspect that segment. If the
user then asks why only one segment was checked, call `list_video_segments`,
locate other "open door" instances, and inspect only semantically relevant new
segments.

## Video Evidence Calling Policy

- For each selected VIDEO item, pass one copied locator (`library_id`, `asset_id`,
  `evidence_id`, original `time_range`) and one focused question.
- Use `fps=auto`, omit `crop`, and use `visual_budget=balanced` unless a caller
  explicitly requests another supported setting. Let AVI decide whether the
  selected range needs higher-resolution frames, a crop, or focused resampling.
- Do not invent or silently rewrite locator boundaries. Select another evidence
  item when broader coverage is needed; the main Agent reconciles claims.
- Avoid overlapping calls for the same asset and evidence. Repeat a call only
  when the prior result failed or an explicit unresolved visual detail justifies
  a focused override; do not cite superseded claims twice.
