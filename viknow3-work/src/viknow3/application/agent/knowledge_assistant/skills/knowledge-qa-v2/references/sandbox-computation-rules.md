# Sandbox Computation Rules

This reference is a sub-protocol for knowledge QA. It guides use of DeepAgents
`execute` when exact computation is needed during an answer.

Computation can transform known inputs into derived results.
It cannot establish source facts by itself. If inputs come from knowledge-base
evidence, the final answer must cite the evidence for those inputs.

## Verified Runtime Boundaries

The ViKnow devcontainer sandbox probe verifies:

- `python3` is available.
- Python standard-library computation works.
- `/workspace/scratch` is writable for temporary work.
- `/workspace/outputs` is writable for user-visible artifacts.
- `/tmp` is writable for short-lived process files.
- `/usr` is read-only.
- Network access is unavailable when sandbox `network_access=false`.
- Long-running commands are stopped by timeout.
- Large output is truncated.
- Python tracebacks are visible for correction.

These are observed runtime boundaries, not a substitute for sandbox
configuration.

## When To Compute

Use `execute` only when exact computation materially improves correctness:

- arithmetic with many steps
- numeric precision with `decimal` or `fractions`
- statistics with `statistics`
- combinatorics or enumeration with `itertools`
- date/time arithmetic with `datetime`
- CSV or JSON parsing with `csv` and `json`
- lightweight relational analysis with `sqlite3`
- formula or small algorithm verification

For simple mental arithmetic, answer directly unless uncertainty is meaningful.

## Python Standard Library Only

Prefer `python3` and Python 3.12 standard library modules such as `math`,
`statistics`, `decimal`, `fractions`, `itertools`, `collections`, `datetime`,
`csv`, `json`, `sqlite3`, `re`, and `pathlib`.

Do not assume `numpy`, `pandas`, `matplotlib`, `scipy`, `sklearn`, internet
access, package installation, or external binaries exist.
Do not run `pip install`, `apt`, `curl`, or `wget` for computation.

## File Rules

- Temporary scripts and intermediate data: `/workspace/scratch`
- User-visible artifacts: `/workspace/outputs`
- Short-lived process temp files: `/tmp`

Prefer a small Python script under `/workspace/scratch` when code is more than
a one-liner. Put reusable result files in `/workspace/outputs` when the user
asks for an artifact.

## Error Handling

After each `execute` call:

- Check the exit code.
- If exit code is non-zero, inspect stderr or traceback.
- Fix the code or input and run again.
- Do not present failed-run output as a computed result.
- If output was truncated, rerun with smaller output or write a result file.

## Answering

- State the computed result clearly.
- Mention the method only when useful.
- State precision, rounding, or assumptions when they affect the result.
- Do not claim external facts from computation alone.

## Command Patterns

Small calculation:

```bash
python3 - <<'PY'
from decimal import Decimal

print(Decimal("0.1") + Decimal("0.2"))
PY
```

Larger calculation:

```bash
mkdir -p /workspace/scratch /workspace/outputs
cat > /workspace/scratch/compute.py <<'PY'
import statistics

values = [1, 2, 3]
print(statistics.mean(values))
PY
python3 /workspace/scratch/compute.py
```

## Answer Plan Inputs And Counts

For a numeric calculation, attach each numeric `verified_value` to its input
slot together with the supporting inspect claim ref. Once every input is
verified, use DeepAgents
`execute` for the calculation. Cite the verified input claims in the final
answer. Do not calculate from values that exist only in search or catalog
output.
For `count`, use `execute` to sum deduplicated `verified_items` quantities,
not chunks, search hits, citations, or inspect calls.
