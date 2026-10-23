# Knowledge QA v2 Optimization Log

This log records evaluated Skill changes. Keep failed experiments so later
updates do not repeat them without new evidence.

## 2026-08-13: Evidence Selection, Three-Round Optimization

### Evaluation Scope

- Dataset: 24 queries and 55 video gold segments restored from
  `evals/citation-accuracy/reports/20260812-full-analysis/audit.json`.
- Service: local ViKnow eval endpoint on port 5174.
- Concurrency: 2. Round 1's six client-timeout cases were rerun at concurrency
  1 with the same Skill, and only failed traces were replaced.
- Baseline Skill: `0.13.0`.
- Final retained Skill: `0.14.0`.

The first table preserves the metrics used during the original experiment.
`Inspect proxy` is the old case-macro average: it combines a matching Inspect
input with an eligible returned marker and is not the report-2 `Correct
Inspect` stage. `Historical citation matches` uses the citation-accuracy
scorer that was available during the experiment.

| Version | Change | Inspect proxy (macro) | Historical citation matches | Avg latency | Avg tool calls | Decision |
|---|---|---:|---:|---:|---:|---|
| 0.13.0 | Baseline candidate audit | 0.196 | 4/55 | 160.6 s | 10.50 | Baseline |
| 0.14.0 | Required evidence scope per answer slot | 0.324 | 8/55 | 121.6 s | 9.46 | Retain |
| 0.15.0 | Resolve source identity before scope lock | 0.337 | 6/55 | 177.4 s | 10.42 | Revert |
| 0.16.0 | Complete every pending locator candidate | 0.238 | 5/55 | 134.6 s | 9.96 | Revert |

### Inspect Funnel Rescore

The saved traces were rescored offline after Skill Flow gained ordered,
per-gold Inspect funnel metrics. The new scorer treats a final time match as
successful when any used citation has the correct asset and both time
boundaries are within five seconds. This intentionally avoids the historical
scorer's behavior of choosing one citation by total boundary error before
checking the two boundary limits, so the final-match counts are comparable
across these four rows but are not identical to the historical table above.

| Version | Candidate exposed | Inspect targeted | Inspect after exposure | Eligible return | Aligned return | Final citation matched | Complete chain | Current funnel hard pass |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.13.0 | 23/55 | 15/55 | 15/23 | 15/15 | 8/15 | 6/55 | 6/8 | 3/24 |
| 0.14.0 | 32/55 | 16/55 | 15/32 | 15/15 | 8/15 | 8/55 | 7/8 | 3/24 |
| 0.15.0 | 26/55 | 17/55 | 17/26 | 17/17 | 5/17 | 6/55 | 4/5 | 3/24 |
| 0.16.0 | 24/55 | 11/55 | 11/24 | 11/11 | 5/11 | 5/55 | 5/5 | 2/24 |

Stage denominators are explicit. Candidate exposure, targeted Inspect, and
final citation matching use all 55 gold segments. Later conditional stages use
the preceding stage's hit count. `Complete chain` requires the aligned Inspect
claim's citation marker to be used by a matching final citation from the same
Inspect modality. `Current funnel hard pass` requires all gold segments to
have a final match, no citation-format error, and a completed run; it does not
assert semantic answer correctness and is not the historical report-2 Hard
metric.

The table was corrected after the scorer learned the actual
`list_video_segments` schema (`metadata.asset_id`, `begin_ts`, and `end_ts`)
and counted gold locators in offloaded results only when the Agent actually
read or grepped those results. The earlier 8-11 candidate counts were parser
undercounts and must not be used.

The corrected rescore sharpens the experiment conclusions:

- Round 1 improved candidate exposure from 23 to 32 gold segments and final
  matches from 6 to 8. It did not increase post-exposure Inspect calls or
  aligned returns: both stayed at 15 and 8. The retained evidence-scope rule's
  measured gain is therefore better retrieval exposure and final selection,
  not a proven improvement in Inspect temporal localization.
- Round 2 produced the most targeted Inspect calls and the highest conversion
  from exposed candidate to Inspect call, but aligned return fell from 8/15 to
  5/17. Its extra source exploration found more windows without reliably
  locating the gold time boundaries, while also increasing latency.
- Round 3's aligned-return conditional rate was 5/11, and its broad
  pending-candidate obligation reduced exposure and targeted Inspect volume.
- Eligible-return conversion was 100 percent in all four runs once a matching
  post-exposure Inspect occurred. For this dataset, tool failure or empty claim
  return is not the main Task B bottleneck; candidate coverage and temporal
  alignment are.
- Every aligned return in these traces propagated through a matching final
  citation marker in Round 3, but not in the other runs. Round 1 propagated
  7/8 aligned returns. Citation-token stability remains Task A and should not
  be conflated with Task B evidence-selection losses.

### Cost and Tool Comparison

All four current runs contain complete performance fields. Token totals are
input plus output tokens across 24 cases. Latency is wall-clock seconds
reported by the eval service.

| Version | Tool calls total / avg | Search + List | Video Inspect | Total tokens | Avg latency | P50 / P95 latency |
|---|---:|---:|---:|---:|---:|---:|
| 0.13.0 | 252 / 10.50 | 114 | 49 | 7,568,075 | 160.6 s | 134.1 / 301.3 s |
| 0.14.0 | 227 / 9.46 | 101 | 45 | 6,988,920 | 121.6 s | 119.0 / 190.0 s |
| 0.15.0 | 250 / 10.42 | 94 | 64 | 7,586,953 | 177.4 s | 173.4 / 344.9 s |
| 0.16.0 | 239 / 9.96 | 115 | 40 | 7,408,605 | 134.6 s | 118.0 / 309.8 s |

Round 1 used 25 fewer tools and about 579 thousand fewer tokens than baseline,
while reducing average latency by 39 seconds and P95 latency by 111 seconds.
Round 2 spent the most tokens and time and made 64 video Inspect calls without
improving aligned-return count, strengthening the revert decision. Round 3
used fewer video Inspect calls but more retrieval calls than Round 1 and still
lost candidate and final coverage.

Report-2 stores tool counts but not token or latency fields. Because the
current service uses Qwen, the relevant comparison is Qwen-only. Report-2's
Qwen old/new groups averaged 5.75/8.00 total tool calls, 2.25/3.17 retrieval
calls, and 1.46/2.21 video Inspect calls per case. Current Round 1 averages
9.46 total calls, 4.21 Search + List calls, and 1.88 video Inspect calls. It
therefore retrieves more often than both Qwen groups while inspecting less
than Qwen new. Token and latency cannot be compared to report-2 from the
available audit artifacts. Report-2 old/new Skills are not the same versions
as current 0.13/0.14, so this is a same-model cross-experiment comparison, not
a direct Skill A/B attribution.

Rescored reports:

- Baseline: `/tmp/viknow2-task-b-v013-funnel-report/`.
- Round 1: `/tmp/viknow2-task-b-round1-v014-funnel-report/`.
- Round 2: `/tmp/viknow2-task-b-round2-v015-funnel-report/`.
- Round 3: `/tmp/viknow2-task-b-round3-v016-funnel-report/`.

### Round 1: Required Evidence Scope

- Source cases: video-scoped questions such as `LARK-CITE-002` were answered
  from inspected document claims after video candidates had been listed.
- Root cause: primary-modality guidance selected an inspect tool by fact type,
  but answer-slot completion did not preserve the source requested by the user.
- Change class: evidence boundary, explicitly approved for this experiment.
- Minimal change: derive each slot's required evidence scope from both fact type
  and requested source; only an eligible claim in that scope can verify it.
  Comparisons between observed behavior and written requirements use separate
  slots.
- Impact: prevents text evidence from closing a video-observation slot while
  still allowing text and video to support distinct parts of one answer.
- Result: inspect recall and strict citation success doubled while latency and
  tool count fell. Retained as Skill `0.14.0`.
- Reports:
  `/tmp/viknow2-task-b-round1-v014-merged-flow-report/` and
  `/tmp/viknow2-task-b-round1-v014-merged-citation-report/`.

### Round 2: Source Identity Before Scope Lock

- Source cases: broad topical similarity routed several questions to a related
  but wrong library or video asset.
- Root cause hypothesis: retrieval scope was locked before distinguishing
  source constraints were resolved.
- Change class: retrieval strategy.
- Minimal change: prefer identifiers and distinguishing constraints over broad
  topical similarity before locking library and asset scope.
- Result: inspect recall improved only marginally, strict passes fell from 8 to
  6, wrong-asset cases remained at 6, and latency increased by 46 percent from
  Round 1. The instruction encouraged additional cross-scope exploration
  without reliably resolving ambiguous source identity.
- Decision: reverted. Source ambiguity in standalone queries must also be
  represented by dataset context or evaluated separately; Skill prompting must
  not guess missing identity.
- Reports: `/tmp/viknow2-task-b-round2-v015-c2-flow-report/` and
  `/tmp/viknow2-task-b-round2-v015-c2-citation-report/`.

### Round 3: Pending Candidate Completion

- Source cases: after `list_video_segments`, agents often returned to search or
  answered without inspecting a listed candidate.
- Root cause hypothesis: the existing `keep_for_inspect` rule did not define
  when a locator call was complete.
- Change class: candidate-selection strategy.
- Minimal change: require every plausible locator result to be inspected or
  rejected before another retrieval, locator call, or answer.
- Result: wrong-asset cases fell from 6 to 4, but inspect recall fell to 0.238,
  strict passes fell to 5, and citation-format errors increased to 9/24. The
  rule created an over-broad obligation and diluted attention across candidates.
- Decision: reverted. Keep the narrower rule that explicitly selected
  `keep_for_inspect` candidates must be consumed before broad retrieval.
- Reports: `/tmp/viknow2-task-b-round3-v016-c2-flow-report/` and
  `/tmp/viknow2-task-b-round3-v016-c2-citation-report/`.

### Remaining Findings

- Standalone questions that only say "the video" can be source-ambiguous even
  when their original dataset record identifies the intended video. Such cases
  should carry source context or be scored separately from Skill routing.
- Accurate temporal-window selection remains the largest Task B gap after the
  retained Skill change. Broad prompt obligations did not improve it reliably.
- Final citation-token generation remains a separate Task A concern and should
  not be used as the sole signal for candidate-selection changes.

## 2026-08-13: Reference Routing And Core-Rule Placement

### Motivation And Baseline

- Source finding: Skill `0.14.0` moved detailed candidate and video guidance to
  `references/`, but it was unknown whether the Agent actually loaded those
  files.
- Scorer change: Skill Flow now records reference read calls, cases, files,
  routing triggers, required reads, and whether each read occurred before its
  dependent tool. The initial deterministic routing rule audits
  `video-candidate-audit.md` before `list_video_segments`; after the experiment
  this remains a diagnostic for comparing traces, not a production requirement.
- Offline rescore: across 96 saved traces, all four evaluated versions had zero
  reference reads. List-triggered cases were 14 for `0.13.0`, 12 for `0.14.0`,
  13 for `0.15.0`, and 13 for `0.16.0`; read and ordering compliance were zero
  in every group.
- Conclusion: prior experiments evaluated the main `SKILL.md`, not behavior
  described only in references.

### Routing Experiment

- Hypothesis: a hard natural-language precondition can make the Agent read the
  video reference before List without causing unrelated reads or List calls.
- `0.14.1`: added a hard precondition using the relative reference path. On
  `LARK-CITE-004`, runtime loaded the new Skill but called List without reading
  the reference. Read and ordering compliance were both 0/1.
- `0.14.2`: changed only the reference path to the directly callable absolute
  workspace path. The first smoke eventually read both video and generic
  candidate references, but only after List: read compliance 1/1, ordering 0/1.
- Six-case sample: three historical List cases and three historical no-List
  controls. Four cases called List; two read the required reference before it.
  Required-read and ordering compliance were both 2/4. One historical no-List
  control was induced to call List, and another read the video reference without
  calling List.
- Decision: reject prompt-only reference routing as a core workflow contract.
  It improved read frequency but missed the 90 percent target, failed ordering
  half the time, and introduced unnecessary behavior.

### Core-Rule Placement Experiment

- Placement decision: the main Skill must contain rules that change tool paths
  or determine whether candidate evidence becomes Inspect evidence. References
  retain detailed examples and uncommon boundary guidance.
- Core rules moved back: semantic video selection by answer slot, overlapping
  event deduplication, bounded candidate sets, List-result selection followed by
  Inspect, and scoped wording for partial coverage. Reference reading is no
  longer a hard precondition.
- `0.14.3` initially required every selected non-duplicate candidate to be
  inspected. In a four-case sample it caused 14 video Inspect calls on
  `LARK-CITE-004`, averaged 12.5 tools per case, and produced only one aligned
  return with no final match. This repeated the over-broad obligation failure
  observed in `0.16.0` and was rejected.
- `0.14.4` replaced that obligation with the smallest non-duplicate set capable
  of covering each slot, capped at five chunks per slot. The
  `LARK-CITE-004` smoke fell from 14 to 4 video Inspect calls and converted all
  four exposed gold windows into eligible Inspect returns, but none were
  temporally aligned and none reached the final answer.
- Current status: `0.14.4` retains the placement correction but is not yet a
  proven temporal-localization improvement. It requires a full 24-query,
  55-gold regression before retention or further strategy changes.

### Full Regression And Final Decision

- Scope: 24 queries and 55 gold segments, concurrency 2. One socket-timeout case
  (`LARK-CITE-010`) was rerun alone and replaced before offline rescoring. All
  final traces completed and loaded Skill `0.14.4`.
- Compared with retained `0.14.0`, `0.14.4` reduced candidate exposure from
  32/55 to 23/55, increased post-exposure Inspect from 15/32 to 17/23, reduced
  aligned returns from 8 to 6, final matches from 8 to 6, complete chains from
  7 to 6, and hard passes from 3/24 to 2/24.
- Cost regressed: tools increased from 227 to 244, total tokens from 6,988,920
  to 7,408,690, average latency from 121.6 to 132.5 seconds, and P95 latency
  from 190.0 to 246.8 seconds.
- List split: List cases increased from 12 to 14. Their aligned and final
  matches improved from 1 to 2, but List-case tool calls rose from 138 to 179.
  The reliable no-List path regressed from 7 aligned/final matches to 4 while
  its case count fell from 12 to 10.
- Root cause: the inline video rules improved consumption after exposure but
  attracted more cases into the expensive List path and displaced direct
  Search-to-Inspect behavior. Better conditional conversion did not compensate
  for lost candidate exposure or no-List regressions.
- Decision: reject `0.14.4` and restore production Skill `0.14.0`. Keep the
  reference-routing scorer and the experiment evidence. Any future core-rule
  placement change must preserve the direct Search-to-Inspect path and should
  first target the trigger boundary separately from post-List consumption.
- Full report:
  `/tmp/viknow2-task-b-inline-core-v0144-full-report/`.

### Artifacts

- Offline routing rescoring:
  `/tmp/viknow2-task-b-v013-reference-routing-report/` through
  `/tmp/viknow2-task-b-v016-reference-routing-report/`.
- Routing smokes and sample:
  `/tmp/viknow2-task-b-routing-v0141-smoke-report/`,
  `/tmp/viknow2-task-b-routing-v0142-smoke-report/`, and
  `/tmp/viknow2-task-b-routing-v0142-sample-report/`.
- Core placement samples:
  `/tmp/viknow2-task-b-inline-core-v0143-sample-report/` and
  `/tmp/viknow2-task-b-inline-core-v0144-smoke-report/`.

## 2026-08-13: Observable Default Evidence Flow

- Hypothesis: replacing internal candidate-state terminology with two explicit,
  observable paths would stabilize behavior: direct
  `Search -> Inspect -> Answer`, and partial-video coverage
  `Search -> List on the same video -> Inspect -> Answer`.
- `0.14.5` retained required evidence scope, described required answer facts
  instead of self-maintained slot states, and inlined both short path examples.
- Six-case gate: three historical List cases and three historical direct-Inspect
  controls. Two List cases timed out at 300 seconds. One direct control trace did
  not expose the loaded Skill version and was rerun; it then also timed out at
  concurrency 1. These failed traces were not scored as behavioral evidence.
- Confirmed `0.14.5` traces showed mixed behavior: `LARK-CITE-020` preserved a
  direct Inspect path; `LARK-CITE-018` used one List followed by one Inspect;
  `LARK-CITE-009` used one List, four video Inspect calls, and 11 total tools;
  `LARK-CITE-021`, previously a no-List success, called List twice and never
  called video Inspect.
- Decision: reject before full regression. The explicit partial-coverage path
  still increased List salience without reliably enforcing post-List Inspect,
  and the gate included regressions and repeated long-running cases. Production
  Skill was restored to `0.14.0`.
- Artifacts:
  `/tmp/viknow2-task-b-observable-flow-v0145-sample-report/` and
  `/tmp/viknow2-task-b-observable-flow-v0145-retry-report/`.

### Candidate-Source Rescore

The initial gate analysis did not distinguish candidate exposure produced by
Search from exposure produced by List. Skill Flow was extended with these
per-gold metrics:

- `search_candidate_exposed` and `list_candidate_exposed`;
- `list_incremental_exposed`: List exposed the gold before any matching Search
  exposure;
- source-specific candidate Inspect counts, including
  `list_incremental_candidate_inspected`.

Offline rescoring changed the interpretation, but not the decision to avoid a
full `0.14.5` run:

- Retained `0.14.0`: Search exposed 11 gold candidates and List exposed 21
  incremental gold candidates. Search candidates reached matching Inspect at
  8/11; List incremental candidates reached matching Inspect at 7/21.
- Rejected `0.14.4`: Search exposure fell to 8, while List exposed 15
  incremental candidates and 10 reached matching Inspect. Its inline rules
  therefore did improve List-candidate consumption, but overall aligned and
  final matches still fell because Search exposure and temporal alignment
  regressed.
- Confirmed completed `0.14.5` traces: `LARK-CITE-009` had one List-incremental
  gold exposure, but none of four video Inspect calls targeted it;
  `LARK-CITE-018` called List without gold exposure; `LARK-CITE-021` added two
  List calls with neither gold exposure nor video Inspect; `LARK-CITE-020`
  preserved its Search-to-Inspect complete chain.

Corrected conclusion: List is often a valuable incremental locator, not merely
an unnecessary call. The main Task B failure is converting a List-exposed gold
window into the correct Inspect target without attracting already successful
Search-to-Inspect cases into List. Future experiments must report candidate
source and incremental List consumption before judging List effectiveness.

Source-rescored reports:

- `/tmp/viknow2-task-b-v014-source-funnel-report/`;
- `/tmp/viknow2-task-b-v0144-source-funnel-report/`;
- `/tmp/viknow2-task-b-v0145-source-funnel-report/`.

### Claim And Final-Usage Coverage Rescore

The strict aligned-return metric requires one eligible claim source to match
both gold boundaries within five seconds. This undercounts Inspect outputs that
split one gold interval into multiple narrower claims. Skill Flow now preserves
that strict metric and adds parallel video metrics:

- A claim locator hit requires positive overlap with the gold and more than 50
  percent of the claim duration inside the gold interval.
- Claim union coverage merges locator-hit intervals after clipping them to the
  gold. Union success requires at least 80 percent gold coverage and no
  uncovered gap greater than five seconds.
- The same metrics are recomputed using only eligible claim markers actually
  present in the final answer. Multiple claims may hit one gold, but gold recall
  counts that gold once.
- Strict, locator-hit, and union-based case passes remain separate. None of
  these metrics asserts claim-text semantic correctness.

Offline results on the complete 24-query, 55-gold runs:

| Version | Strict single-claim aligned | Gold with claim locator hit | Claim union covered | Gold with used claim locator hit | Used claim union covered | Strict / used-locator / used-union case pass |
|---|---:|---:|---:|---:|---:|---:|
| `0.14.0` | 8 | 10 | 5 | 8 | 4 | 3 / 2 / 1 |
| `0.14.4` | 6 | 13 | 5 | 10 | 5 | 2 / 3 / 1 |

The revised metrics show that `0.14.4` did improve partial claim localization
and final use of locally relevant claims, which the strict metric understated.
It did not improve complete union coverage or the all-gold used-union case pass,
while its tool count, tokens, and latency were higher. Therefore the evidence
does not justify restoring `0.14.4` as-is, but its List-consumption instruction
contains a real local gain that should be isolated from its Search-exposure and
cost regressions in the next experiment.

Claim-coverage reports:

- `/tmp/viknow2-task-b-v014-claim-coverage-report/`;
- `/tmp/viknow2-task-b-v0144-claim-coverage-report/`;
- `/tmp/viknow2-task-b-v0145-claim-coverage-report/`.

## 2026-08-13: Stage Three List Post-Result Guidance

- Stage target: improve conversion from List-incremental gold exposure to the
  correct video Inspect target without changing the main Skill or the decision
  to call List.
- Change: narrowed `list_video_segments.next_action.instruction` to preserve
  relevant Search evidence, select only the smallest listed set for information
  still missing from the question, Inspect exact returned boundaries before
  further retrieval or answering, and avoid inspecting every segment. Tool
  schema, segment results, List trigger policy, and Skill `0.14.0` were unchanged.
- Dataset gate: seven `0.14.0` cases with List-incremental gold exposure not
  fully consumed, plus two controls whose incremental candidates were consumed.
- Smoke `LARK-CITE-002` confirmed the new instruction reached the trace and
  changed the observed path from zero video Inspect calls in the baseline to one
  video Inspect call after List. This is behavioral evidence only; gold targeting
  still requires scorer comparison.
- Batch status: the first concurrency-2 batch produced one completed direct
  Search-to-Inspect trace, one partial failed trace that saw the new instruction,
  then 504, connection, and repeated 502 failures. A concurrency-1 retry of all
  seven failed cases returned immediate 502 responses. These failures are an
  eval-service outage and are excluded from Skill/tool-effect conclusions.
- Decision: keep the implementation pending a clean rerun after the local 5174
  eval service is restarted. Do not retain or revert based on the failed batch.
- Artifacts:
  `/tmp/viknow2-task-b-list-next-action-smoke-report/`,
  `/tmp/viknow2-task-b-list-next-action-sample-report/`, and
  `/tmp/viknow2-task-b-list-next-action-retry-report/`.
