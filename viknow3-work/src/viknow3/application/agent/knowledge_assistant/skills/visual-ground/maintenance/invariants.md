# Visual Ground Invariants

These invariants define the behavior boundary for `visual-ground`. Automated
optimization may clarify wording and examples, but must not weaken these
contracts. Changes to this file require explicit human review.

## Evidence Scope

- Video grounding uses exactly one selected frame and requires an
  evidence-backed `frame_time`; never fabricate a timestamp.
- A result verifies only its selected image or video frame. Do not infer a
  video-wide total, a final state, or absence from another frame.
- If no frame can support the user's requested temporal meaning, state
  insufficiency instead of inferring a count or location.

## Target and Result Integrity

- `query` must identify one focused visual target and the requested count or
  location. Do not use a bare noun, unrelated context, or the whole
  conversation.
- When similar objects could be confused, qualify the target with
  evidence-backed distinguishing attributes.
- Report exactly the returned detections and count for the selected frame; do
  not fabricate, average, maximize, or otherwise aggregate results across
  frames.
