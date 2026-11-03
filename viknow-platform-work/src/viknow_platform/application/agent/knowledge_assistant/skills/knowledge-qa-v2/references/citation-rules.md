# Citation Rules

All citations must come from inspect tool `claims`. Search results and
locator outputs are never citable.

`visual_ground` claims are citable only for visual grounding and object-location
facts. Its raw detections, extracted frames, sampled timestamps, and
search/locator inputs are not citable by themselves.

When the final answer gives a visible object/category count and a
`visual_ground` claim exists for that count, cite the `visual_ground` claim
beside the count sentence. Do not cite only the earlier `video_inspect_2_0`,
`knowledge_search`, or `list_video_segments` evidence for the same count.

## Current-Run Claims

Copy the exact `agent_citation` token returned by the tool, such as `[1]` or
`[2]`. Do not renumber, merge, or rewrite citation tokens.

## Historical Claims

When using a historical verified claim, copy its stable `citation_token`
exactly, for example `[claim:<run_id>:<tool_call_id>:<claim_id>]`.

## Claim Eligibility

Do not cite:

- failed tool results;
- claims with `confidence <= 0`;
- `text_inspect` claims where `quote_verified == false`;
- raw `visual_ground` detections, extracted frames, or per-frame results unless
  they were summarized into a returned claim;
- raw `knowledge_search` text, captions, indexed transcripts, indexed
  visible-text snippets, or `list_video_segments` content.

If a fact has no eligible claim, say evidence is insufficient for that fact.
