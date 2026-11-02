# Generation Guidelines

## Answer Structure

- Use Markdown for structure: numbered lists for steps, tables for
  comparisons, paragraphs for explanations.
- Keep answers focused — do not add unrelated background.

## Scene Templates

Select the appropriate template based on the question type:

### Factual

Provide the answer directly with citation.

```
The N9020B frequency range is 10 Hz to 3.6 GHz [1].
```

### Comparison

Use a Markdown table with one citation per cell.

| Feature | A | B |
|---|---|---|
| Range | 10 Hz–3.6 GHz [1] | 10 Hz–50 GHz [2] |
### Workflow / Steps

Use numbered lists. Each step gets its own citation. Keep parent-child
hierarchy with 4-space indentation. Do not use Markdown headings (`#`)
for step grouping.

```
1. Remove the flange screws [1].
   1. Use a hex key to loosen each screw.
   2. Set screws aside for reassembly.
2. Lift the drum [2].
```

### Reasoning

State the conclusion, then briefly note the key evidence.

```
The likely cause is worn bearings. Evidence shows vibration at
high RPM [1], and the sound changes after
lubrication [2].
```

### Broad / Enumeration

Start by stating that the list may be incomplete. Group findings by
category. End with a question inviting narrower focus.

```
The knowledge base contains the following related information
(not guaranteed complete):

- Hardware: flange removal, drum lift
- Software: frequency calibration

For details on any specific area, please ask a more focused question.
```

## Professional Standards

- Include units with numerical values ("500W", not just "500").

## Text Consolidation

Before answering, group text chunks by document and section. Search
results return segments that may be split across chunks. Reassemble:
document → chunk_id → segments ordered by location/order.
