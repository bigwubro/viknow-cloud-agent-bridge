# Inspect Results Reference

Read when an inspect result's claim eligibility is unclear. The root Skill owns
the answer decision; this file describes how to interpret tool output, not an
additional reason to keep searching.

| Tool | Verifies | Boundary |
|---|---|---|
| `document_inspect` | Claims from the selected source document page, including text, tables, figures and charts | Use only returned claims; a page does not prove uninspected pages or the entire asset. |
| `video_inspect` | Claims about the selected video time range and reliable frame times | Does not verify exact speech. Assess each claim against the requested fact, unit and scope. |
| `visual_ground` | Count or location of visible targets in one image or a supported video frame | Does not establish an unobserved time range or source-wide total. |

For every inspect result, discard a failed result, an empty `claims` list, and
individual claims with `confidence <= 0`. Match each remaining claim's subject,
predicate, period, unit and scope to the original question. A successful tool
call is not proof that the requested fact was verified. A claim about an
adjacent page, model or attribute remains a different fact.

Copy only the returned `agent_citation` token for an eligible claim. Raw
detections, extracted frames, locator summaries and tool metadata are not
citable claims. If no eligible claim covers a required binding element, keep
that slot open and follow the root Skill's pre-answer verify gate.
