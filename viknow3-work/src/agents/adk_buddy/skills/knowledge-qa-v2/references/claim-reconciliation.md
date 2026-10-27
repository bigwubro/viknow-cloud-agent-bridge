# Claim Reconciliation

Use this reference before final answer synthesis when multiple tools return
claims about the same time range, asset, object, or event.

## Step 1: Discard Unusable Claims

Ignore failed tool results and claims with `confidence <= 0`. For documents,
use only claims actually returned by `document_inspect`.

## Step 2: Compare Fact And Scope

Two claims conflict only when they assert incompatible values for the same fact
under the same modality scope.

Common scopes:

- **visual**: what is visible in the frame.
- **speech**: what is spoken or heard in the audio, only when already available
  as an eligible historical claim or verified text claim.
- **visible text**: what readable text in the image/video says.
- **document**: what a written source prescribes or states.

Different scopes can be simultaneously true. For example:

- `video_inspect`: "No rescuer or rescued person is visible in the frame."
- eligible historical audio claim: "The speaker identifies themselves and
  reassures the passenger."

These are not contradictory. The combined conclusion is that the frame does not
show face-to-face rescue interaction, while eligible historical evidence says
the audio contains reassurance speech.

## Step 3: Resolve True Conflicts

When claims conflict on the same fact and scope:

- Speech wording, dialogue, announcements, reassurance, or instructions: use
  only eligible claims that directly verify the audio fact; otherwise state
  insufficiency.
- Visible actions, counts, posture, object state, or scene content: prefer
  `video_inspect` for actions and state, and `visual_ground` for visible
  target counts and locations.
- Visible text, labels, signs, screens, or panels: prefer enabled inspect claims
  that explicitly read the text.
- SOP requirements, parameters, or procedure text: prefer `document_inspect`
  claims from the requested source.

If the primary modality also has conflicting eligible claims, state that the
evidence conflicts instead of forcing a single answer.

## Step 4: Preserve Boundaries In The Answer

Write what each source proves without overextending it:

- Good: "The frame does not show face-to-face interaction, but the audio shows
  the operator reassuring the passenger by identifying themselves and saying not
  to panic."
- Bad: "The video shows the operator reassuring the passenger face to face" when
  only a separate speech claim supports the reassurance.
