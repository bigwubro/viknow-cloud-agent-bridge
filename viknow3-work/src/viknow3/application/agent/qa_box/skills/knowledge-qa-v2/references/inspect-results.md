# Inspect Results Reference

Inspect-style tools return `{success, error, claims: [...]}`.

## Claim Fields by Tool

| Field | video_inspect_2_0 | video_counter | image_inspect | text_inspect |
|---|---|---|---|---|
| `claim` | What was observed in video | Visual grounding/location from sampled frames | What was observed in image | Fact stated in source text |
| `confidence` | Observation confidence (0-1) | Nonzero-count agreement score (0-1) | Stage-2 self-check score (0-1) | Extraction confidence (0-1) |
| `verify_reason` | Observation/refinement basis | Frame-count aggregation reason | Why VLM gave that score | Why quote matched or failed |
| Source field | `timestamp_start/end` | `timestamp_start/end` | `source_image_id` | `source_segments` |
| Verification | — | Nonzero mode over sampled frame detections | — | `supporting_quote`, `quote_verified`, `match_ratio` |

## Usage Rules

- `text_inspect` and `image_inspect` must be called with the selected
  search evidence item's `id` as `evidence_id`, together with
  `library_id` and `asset_id`.
- Discard claims where `quote_verified == false` (text_inspect only).
- Discard claims where `result.success == false` (all tools).
- Higher confidence is better. Cross-check against `verify_reason`.
- Empty `claims` list → evidence does not support the question.
