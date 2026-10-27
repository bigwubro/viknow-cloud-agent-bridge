# Generation Guidelines

## Answer Structure

Begin with `Answer:` or `答案：` and the requested fact, supported items, or
what cannot be determined. Use Markdown only when it clarifies a requested
comparison or sequence. Keep the answer within the original question's scope;
do not add related facts just because they were retrieved.

## Scene Templates

Select the appropriate template based on the question type:

### Factual

Provide the answer directly with citation.

```
Answer: The N9020B frequency range is 10 Hz to 3.6 GHz [C1].
```

### Comparison

Use a Markdown table when it makes the requested comparison clearer. Cite each
evidence-dependent value; do not fill a missing cell with a nearby model or
attribute.

Answer: The requested comparison is below.

| Feature | A | B |
|---|---|---|
| Range | 10 Hz–3.6 GHz [C1] | 10 Hz–50 GHz [C2] |
### Workflow / Steps

Use numbered lists for verified steps. Cite each evidence-dependent step.
If a required step is not verified, name that limitation instead of completing
the procedure from general knowledge.

```
Answer: The verified steps are:

1. Remove the flange screws [C1].
   1. Use a hex key to loosen each screw [C1].
   2. Set screws aside for reassembly [C1].
2. Lift the drum [C2].
```

### Reasoning

State the supported conclusion first, then briefly note the key evidence.
Do not convert a plausible explanation into an established cause.

```
Answer: The supported cause is worn bearings. Evidence shows vibration at
high RPM [C1], and the sound changes after
lubrication [C2].
```

### Broad / Enumeration

If full-source coverage is verified, list the complete qualifying set. If it
is not verified, lead with the fact that the requested complete list cannot be
determined, then label only the verified subset or lower bound and state the
uncovered scope. Do not imply a complete answer from top-k hits or append an
unrequested invitation to broaden the question.

```
Answer: The complete list cannot be determined from the verified coverage.
The verified subset is:

- Hardware: flange removal [C1], drum lift [C2]
- Software: frequency calibration [C3]

This does not establish that the full source has been covered.
```

## Professional Standards

- Include units with numerical values ("500W", not just "500").

## Text Consolidation

Search chunks may point to different pages or sections. Use them to select
`document_inspect` locators; do not consolidate raw search text into a final
factual answer. Keep document, page and section scope distinct when comparing
eligible claims.
