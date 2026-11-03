# Evidence Modality Policy

Use this reference when a question may require multiple modalities or when tool
claims conflict.

## Primary Modality

Pick the source of truth from the fact type, not from keyword matching:

- **Video**: visible actions, people counts, object state changes, posture,
  scene layout, tool use, readable visible text when the enabled inspect tool
  can verify it, and whether an event happened visually.
- **Text**: SOP rules, written steps, parameters, specifications, document
  statements, and policy requirements.
- **Unavailable speech/audio facts**: exact spoken wording, dialogue, narration,
  alarms, voice commands, announcements, or reassurance are not directly
  citable unless represented in selected text evidence and verified by
  `text_inspect`.

## Conflict Policy

- For speech/audio wording, do not infer exact words from visual inspection.
  Use transcript snippets only as locator hints and state insufficiency if no
  enabled inspect tool verifies the fact.
- For visible action/count/state, `video_inspect_2_0` overrides locator hints.
- For visible text, cite only text that an enabled inspect tool verifies.
- For SOP/document requirements, `text_inspect` overrides video-derived guesses.
- Discard failed results and `confidence <= 0` claims before resolving conflicts.

## Examples

- User asks: "操作者说了什么来安抚乘客？" Exact speech is unavailable unless
  selected text evidence can be verified; otherwise state insufficiency.
- User asks: "门打开后对面有几个人？" Primary: video. Transcript snippets can
  help locate the moment but cannot answer the visual grounding.
- User asks: "屏幕上显示什么报警码？" Primary: image/video only if the enabled
  inspect tool can read the code as a verified claim.
- User asks: "SOP 要求第七步是什么？" Primary: text.

## Tool Capability Boundaries

- `uploaded_image_search` analyzes one user-uploaded image before text query
  refinement. Use it only for workspace uploads, not for image Evidence returned
  by `knowledge_search`.
- `video_inspect_2_0` extracts traceable claims from one selected video
  evidence range. It may adapt frame resolution, crop a relevant region, or
  increase sampling around an uncertain moment. It does not transcribe speech,
  verify exact audio wording, search other ranges, or combine multiple clips.
- visual_ground verifies object counts and spatial grounding from images or
  sampled video frames. For video, it samples the selected evidence time range
  and returns inspect-style claims. It does not search for candidate evidence and
  does not replace `video_inspect_2_0`.
- For speech, dialogue, reassurance, reminders, announcements, or audio
  instructions, use indexed transcript snippets only as locator hints. If no
  enabled inspect tool can verify the fact as a claim, state that evidence is
  insufficient.
- For visible text in media, use visual text snippets only as locator hints.
  Verify only what an enabled inspect tool can read as a claim; otherwise state
  that evidence is insufficient.
- For document/SOP text, call `text_inspect` only on evidence that has a TEXT
  part copied from `knowledge_search` or a segment locator returned by
  `list_doc_segments(mode=select)`.
