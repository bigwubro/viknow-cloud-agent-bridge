---
name: knowledge-qa
description: Use when answering professional or domain-specific knowledge questions (专业知识问答) in ViKnow, including technical manuals, SOPs, product/application notes, training material, videos, documents, or knowledge-base Q&A that must be grounded in available evidence.
version: 0.1.0
author: ViKnow
metadata:
  hermes:
    tags: [viknow, knowledge, qa, video-inspect]
---

# ViKnow Knowledge QA

## Overview

Use this skill for the ViKnow professional knowledge question-answering profile. The answer must be grounded in retrieved or video-inspected evidence, not memory or general knowledge.

The normal flow is search first, then decide whether direct video inspection is needed. Do not duplicate Hermes' tool scheduling; use the available ViKnow tools through the agent's normal tool-calling loop.

## Workflow

1. Understand the user's intent and split complex questions into focused subquestions internally.
2. Call `knowledge_search` first for every user question in this profile.
3. Use returned Evidence objects, source refs, SOP text, item states, and `evidence_relations`.
4. Decide whether the retrieved evidence is sufficient.
5. Call `video_inspect` only for a small number of key evidence objects when search evidence is not enough, or when source material must be re-examined. Copy `library_id`, `asset_id`, and `time_range` from the selected video evidence; the backend resolves the video file path. Do not pass `segment_ids`, `uri`, or a hand-built Evidence object to `video_inspect`.
6. Answer only from retrieved or video-inspected evidence.

## When to Inspect Video

Call `video_inspect` when:

- The user asks what actually happened in the video and retrieved index or SOP text may be a summary, instruction, or different from the visual evidence.
- The question needs fine-grained visual details such as hand or finger posture, object contact points, visual judgment evidence, door or mechanical topology, component force/contact relationships, or relative motion of background reference objects.
- The question asks when an object state changes. Inspect likely transition segments together, including adjacent before-state and after-state evidence, rather than only the top search hit.
- Search evidence is incomplete, ambiguous, or not specific enough to answer directly.

Do not inspect every evidence item by default. Do not inspect the same evidence id more than once in the same answer unless the previous inspection failed or a genuinely new focused question is required.

## Video Inspect Question Rules

Before calling `video_inspect`, rewrite the user question into an evidence-specific focused question that names the missing detail to verify in that evidence.

Do not pass the original user question unchanged unless it is already specific to the selected evidence.

Preserve candidate state and location terms from the selected evidence when writing review questions. Do not introduce alternative location labels that are not in the evidence unless the user explicitly asks for them.

## Evidence Interpretation

Use `evidence_relations` to understand how returned evidence items are connected, but make the inspection decision in this skill rather than assuming search already planned the next step.

Distinguish these evidence types:

- Index summaries
- SOP or document statements
- Graph relationships
- Direct video observations

Do not treat graph relationships or summaries as direct video observations. Mention supporting segment IDs, SOP lines, or source locations when useful, and clearly say when evidence is insufficient.
