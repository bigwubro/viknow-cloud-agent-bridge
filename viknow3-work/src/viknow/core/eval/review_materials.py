"""Pure builders for offline Gate A tasks and human Gate B review materials."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from viknow.core.eval.candidate_evidence import CandidateEvidencePackage, EvidenceCollectionTask
from viknow.core.eval.evidence_oracle import EvidenceOracleScope
from viknow.core.eval.research_oracle import ResearchSeed

ReviewScenario = Literal[
    "online_no_annotation_no_answer",
    "dataset_no_annotation_with_answer",
    "dataset_with_annotation_with_answer",
]


class GateACandidate(BaseModel):
    """A local, deterministic Gate-A proposal; it never stores a reference answer."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    material_revision: str = Field(min_length=1)
    source_run_ids: list[str] = Field(min_length=1)
    scope: EvidenceOracleScope
    decision_ref: str = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)
    research_seeds: list[ResearchSeed] = Field(default_factory=list)


class GateBReviewMaterial(BaseModel):
    """Human-facing content separated from the Oracle task input."""

    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]
    markdown: str


def build_gate_a_candidate(
    *,
    case: Mapping[str, Any],
    source_trace: Mapping[str, Any],
    asset_mapping: Mapping[str, Any],
    index_revision: str,
    material_revision: str,
) -> GateACandidate:
    """Build fixed scope and reinspection seeds without reading reference answers."""

    case_id = _required_text(case, "case_id")
    run_id = _required_text(source_trace, "run_id")
    library_id, asset_ids = _resolve_scope(case, asset_mapping)
    reason_codes = ["selected_knowledge_asset"]
    return GateACandidate(
        case_id=case_id,
        material_revision=material_revision,
        source_run_ids=[run_id],
        scope=EvidenceOracleScope(
            library_id=library_id,
            asset_ids=asset_ids,
            asset_revisions={asset_id: index_revision for asset_id in asset_ids},
        ),
        decision_ref=f"review://gate-a/offline/{case_id}",
        reason_codes=reason_codes,
        research_seeds=[],
    )


def build_oracle_task(candidate: GateACandidate) -> EvidenceCollectionTask:
    """Convert an approved local candidate to a blind, scope-limited Oracle task."""

    return EvidenceCollectionTask(
        task_id=f"{candidate.case_id.lower()}-oracle",
        case_id=candidate.case_id,
        material_revision=candidate.material_revision,
        source_run_ids=candidate.source_run_ids,
        scope=candidate.scope,
        gate_a_status="approved",
        gate_a_decision_ref=candidate.decision_ref,
        research_seeds=candidate.research_seeds,
    )


def render_gate_b_review(
    *,
    case: Mapping[str, Any],
    source_trace: Mapping[str, Any],
    scenario: ReviewScenario,
    candidate_package: CandidateEvidencePackage | None,
    human_annotation: Mapping[str, Any] | None = None,
) -> GateBReviewMaterial:
    """Render review context; reference answers are visible only in dataset scenarios."""

    case_id = _required_text(case, "case_id")
    reference_answer = _optional_text(case.get("reference_answer"))
    if candidate_package is not None:
        if candidate_package.task.case_id != case_id:
            raise ValueError("review case must match candidate case")
        if _required_text(source_trace, "run_id") not in candidate_package.task.source_run_ids:
            raise ValueError("review QA run must match candidate source run")
    visible_reference = reference_answer if scenario != "online_no_annotation_no_answer" else None
    visible_annotation = (
        dict(human_annotation)
        if scenario == "dataset_with_annotation_with_answer" and human_annotation is not None
        else None
    )
    citations = _citations(source_trace)
    package = candidate_package.model_dump(mode="json") if candidate_package else None
    payload = {
        "schema_version": "viknow.gate-b-review.v1",
        "case_id": case_id,
        "scenario": scenario,
        "question": _required_text(case, "question"),
        "source_run_id": _required_text(source_trace, "run_id"),
        "qa_answer": _optional_text(source_trace.get("answer")),
        "qa_citations": citations,
        "reference_answer": visible_reference,
        "candidate_evidence_package": package,
        "human_annotation": visible_annotation,
        "gate_b_status": "pending",
    }
    return GateBReviewMaterial(payload=payload, markdown=_render_markdown(payload))


def _resolve_scope(
    case: Mapping[str, Any],
    asset_mapping: Mapping[str, Any],
) -> tuple[str, list[str]]:
    metadata = case.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    selected = metadata.get("selected_knowledge_assets")
    selected = selected if isinstance(selected, list) else []
    assets = [item for item in selected if isinstance(item, Mapping)]
    if assets:
        library_id = _required_text(assets[0], "library_id")
        asset_ids = [_required_text(item, "asset_id") for item in assets]
        if any(_required_text(item, "library_id") != library_id for item in assets):
            raise ValueError("selected knowledge assets must use one library")
        return library_id, list(dict.fromkeys(asset_ids))
    source_video_id = _required_text(metadata, "source_video_id")
    mapped = asset_mapping.get(source_video_id)
    if not isinstance(mapped, Mapping):
        raise ValueError("case has no selected asset or asset mapping")
    return _required_text(mapped, "library_id"), [_required_text(mapped, "asset_id")]


def _citations(trace: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    metadata = trace.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    raw = metadata.get("citations")
    raw = raw if isinstance(raw, Mapping) else {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, Mapping)}


def _render_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        f"# Gate B 人工审核：{payload['case_id']}",
        "",
        f"- 场景：{payload['scenario']}",
        f"- QA source run：`{payload['source_run_id']}`",
        "- Gate B：**pending**",
        "",
        "## 原问题",
        "",
        str(payload["question"]),
        "",
        "## QA 实际回答（仅供定位）",
        "",
        str(payload.get("qa_answer") or "（无）"),
        "",
        "## QA citation-to-locator",
        "",
        "```json",
        json.dumps(payload["qa_citations"], ensure_ascii=False, indent=2, sort_keys=True),
        "```",
    ]
    if payload.get("reference_answer"):
        lines.extend(
            ["", "## 数据集参考答案（不进入 Oracle 输入）", "", str(payload["reference_answer"])]
        )
    if payload.get("human_annotation") is not None:
        lines.extend(
            [
                "",
                "## 人工标注（不进入 Oracle 输入）",
                "",
                "```json",
                json.dumps(payload["human_annotation"], ensure_ascii=False, indent=2),
                "```",
            ]
        )
    lines.extend(_render_candidate_events(payload.get("candidate_evidence_package")))
    lines.extend(
        [
            "",
            "## 完整结构化数据",
            "",
            "完整结构化数据见同目录 `gate-b.json`；Markdown 不重复展示完整 Oracle 候选包。",
            "",
            "## 审核结论（人工填写）",
            "",
            "- [ ] accepted / revised / rejected",
            "- decision_ref：`review://gate-b/...`",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_candidate_events(package: Mapping[str, Any] | None) -> list[str]:
    """Render every persisted event, including exclusions, without model summaries."""
    if package is None:
        return []
    state = package.get("research_state")
    if state is None:
        return ["", "## 候选事件审核", "", "无结构化事件记录；不能推断已完成全量调查。"]
    events = state["events"]
    counts = Counter(event["status"] for event in events)
    lines = [
        "",
        "## 候选事件审核",
        "",
        f"共 {len(events)} 个事件："
        + "；".join(f"{status}={count}" for status, count in sorted(counts.items())),
        "",
        "以下包含待定与被排除事件；事件数不等于答案次数，也不等于工具调用次数。",
    ]
    for event in events:
        window = event["time_range"]
        lines.extend(
            [
                "",
                f"### {event['event_id']} — {event['status']}",
                "",
                f"- 时间：{window['start_seconds']:g}–{window['end_seconds']:g}s",
                f"- 子问题：{', '.join(event['requirement_ids'])}",
                f"- 核验目标：{'；'.join(event['verification_targets'])}",
                f"- 主 Agent 裁决理由：{event.get('rationale') or '未记录'}",
            ]
        )
        for locator in event["locators"]:
            lines.append(
                f"- 定位：{locator.get('library_id', '')} / "
                f"{locator.get('asset_id', '')} / {locator.get('segment_id', '')}"
            )
            if locator.get("match_excerpt"):
                lines.append(f"- 索引候选信息（非人工确认）：{locator['match_excerpt']}")
        reports = [
            report
            for report in state["inspection_reports"]
            if report["event_id"] == event["event_id"]
        ]
        if not reports:
            lines.append("- 包内未附 Inspector 报告；不能据此确认核验内容。")
        for report in reports:
            lines.extend(
                [
                    "",
                    f"Inspector 报告 `{report['task_call_id']}`：{report['status']}",
                    "",
                ]
            )
            if report.get("challenge"):
                lines.append(f"- 本次质疑：{report['challenge']}")
            for finding in report["findings"]:
                lines.extend(
                    [
                        f"- 核验命题：{finding['verification_target']}",
                        f"- Inspector 判断：{finding['disposition']}",
                        f"- 观察：{'；'.join(finding['observed_facts']) or '未记录'}",
                        f"- 限制：{'；'.join(finding['limitations']) or '未记录'}",
                    ]
                )
            if report["coverage_gaps"]:
                lines.append(f"- 未完成项：{'；'.join(report['coverage_gaps'])}")
        lines.extend(
            [
                "",
                "人工判断：待填写（支持 / 排除 / 信息不足）",
                "",
                "修订事实、时间范围与理由：待填写。",
            ]
        )
    return lines


def _required_text(value: Mapping[str, Any], key: str) -> str:
    result = _optional_text(value.get(key))
    if not result:
        raise ValueError(f"{key} is required")
    return result


def _optional_text(value: object) -> str | None:
    result = str(value or "").strip()
    return result or None
