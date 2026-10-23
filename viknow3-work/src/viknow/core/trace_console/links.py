"""External diagnostics links; Jaeger lookup runs only on explicit request."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx

from viknow.core.observability.langfuse_project_identity import LangfuseProjectIdentity
from viknow.core.trace_console.models import (
    ExternalLink,
    TraceConsoleConfig,
    TraceDetail,
    TraceLinks,
    TraceRow,
)


def langfuse_links(
    detail: TraceDetail,
    config: TraceConsoleConfig,
    identity: LangfuseProjectIdentity | None = None,
) -> TraceLinks:
    if not detail.langfuse_traces:
        messages = {
            "historical_trace_source_unknown": "历史 Run 未记录 Langfuse 来源，无法安全跳转",
            "invalid_trace_source_identity": "Run 中的 Langfuse 来源身份不完整，无法安全跳转",
            "project_identity_unresolved": "Langfuse 项目身份暂未确认，无法安全跳转",
        }
        if detail.langfuse_reason in messages:
            return TraceLinks(state="unavailable", message=messages[detail.langfuse_reason])
        return TraceLinks(
            state="not_found",
            message=detail.langfuse_reason or "该运行尚未记录 Langfuse trace",
        )
    if any(not trace.source_id or not trace.project_id for trace in detail.langfuse_traces):
        return TraceLinks(
            state="unavailable", message="历史 Run 未记录 Langfuse 来源，无法安全跳转"
        )
    resolved = []
    for trace in detail.langfuse_traces:
        source = config.langfuse_sources.get(trace.source_id or "")
        if (
            source is None
            and identity is not None
            and identity.project_id == trace.project_id == trace.source_id
        ):
            ui_url = identity.host
            project = quote(trace.project_id, safe="")
            resolved.append(
                ExternalLink(
                    trace_id=trace.trace_id,
                    url=f"{ui_url}/project/{project}/traces/{trace.trace_id}",
                )
            )
            continue
        if source is None:
            return TraceLinks(
                state="not_configured",
                message=f"Langfuse 来源 {trace.source_id} 尚未配置页面地址",
            )
        if source.project_id != trace.project_id:
            return TraceLinks(
                state="unavailable",
                message=f"Langfuse 来源 {trace.source_id} 的项目配置与 Run 记录不一致",
            )
        ui_url = source.resolve_ui_url()
        if ui_url is None:
            return TraceLinks(
                state="not_configured",
                message=f"Langfuse 来源 {trace.source_id} 尚未配置页面地址",
            )
        project = quote(trace.project_id, safe="")
        resolved.append(
            ExternalLink(
                trace_id=trace.trace_id,
                url=f"{ui_url}/project/{project}/traces/{trace.trace_id}",
            )
        )
    return TraceLinks(
        state="multiple" if len(resolved) > 1 else "resolved",
        links=resolved,
    )


def jaeger_links(
    run: TraceRow, config: TraceConsoleConfig, *, client: httpx.Client | None = None
) -> TraceLinks:
    if not config.jaeger_query_url or not config.jaeger_ui_url or not config.jaeger_services:
        return TraceLinks(
            state="not_configured", message="尚未配置 Jaeger 查询地址、页面地址和服务名称"
        )
    if client is None:
        with httpx.Client(timeout=config.query_timeout_seconds, trust_env=False) as owned:
            return jaeger_links(run, config, client=owned)
    ids = []
    try:
        for service in config.jaeger_services:
            response = client.get(
                f"{config.jaeger_query_url}/api/traces",
                params={
                    "service": service,
                    "operation": "viknow.run",
                    "tags": json.dumps({"viknow.run_id": run.id}, separators=(",", ":")),
                    "start": int((run.started_at - timedelta(minutes=1)).timestamp() * 1_000_000),
                    "end": int(
                        ((run.ended_at or datetime.now(UTC)) + timedelta(minutes=1)).timestamp()
                        * 1_000_000
                    ),
                    "limit": 20,
                },
            )
            response.raise_for_status()
            data = response.json()
            if data.get("errors"):
                raise ValueError("Jaeger query failed")
            for trace in data["data"]:
                id = trace["traceID"]
                if (
                    re.fullmatch(r"[0-9a-f]{16}(?:[0-9a-f]{16})?", id)
                    and any(
                        span.get("operationName") == "viknow.run"
                        and any(
                            tag.get("key") == "viknow.run_id" and tag.get("value") == run.id
                            for tag in span.get("tags", [])
                        )
                        for span in trace.get("spans", [])
                    )
                    and id not in ids
                ):
                    ids.append(id)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        return TraceLinks(state="unavailable", message="Jaeger 查询暂不可用，请稍后重试")
    return TraceLinks(
        state="multiple" if len(ids) > 1 else "resolved" if ids else "not_found",
        links=[ExternalLink(trace_id=id, url=f"{config.jaeger_ui_url}/trace/{id}") for id in ids],
        message=None if ids else "未找到对应 trace，可能未采样或已过保留期",
    )
