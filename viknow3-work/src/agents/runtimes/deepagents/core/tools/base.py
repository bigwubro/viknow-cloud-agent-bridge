"""Shared tool base class and result envelope."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agents.runtimes.deepagents.core.tools.domain import ToolSpec
from agents.runtimes.deepagents.core.tools.executor import run_tool_sync


class ToolStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class ToolErrorCode(StrEnum):
    OK = "ok"
    INVALID_ARGUMENTS = "invalid_arguments"
    INVALID_EVIDENCE = "invalid_evidence"
    INVALID_LOCATOR = "invalid_locator"
    INVALID_REVIEW_REQUEST = "invalid_review_request"
    INVALID_ASR_REQUEST = "invalid_asr_request"
    INVALID_OCR_REQUEST = "invalid_ocr_request"
    INVALID_CLIP_SEARCH = "invalid_clip_search_request"
    INVALID_LOCATE_REQUEST = "invalid_locate_anything_request"
    UNKNOWN_LIBRARY = "unknown_library"
    NOT_CONFIGURED = "not_configured"
    UPSTREAM_ERROR = "upstream_error"
    TIMEOUT = "execution_timeout"
    QUEUE_TIMEOUT = "queue_timeout"
    BUDGET_CANCELLED = "run_budget_cancelled"
    GATE_BLOCKED = "tool_gate_blocked"
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED = "unsupported"
    INTERNAL_ERROR = "internal_error"


_ERROR_CODE_ALIASES: dict[str, ToolErrorCode] = {
    "invalid_video_locator": ToolErrorCode.INVALID_LOCATOR,
    "invalid_video_locators": ToolErrorCode.INVALID_LOCATOR,
    "unsupported_count_mode": ToolErrorCode.UNSUPPORTED,
    "cannot_process_image": ToolErrorCode.UPSTREAM_ERROR,
}


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ToolErrorCode
    message: str
    detail: str | None = None
    retryable: bool = False


class ToolBudgetUsage(BaseModel):
    """Runtime budget check snapshot; recursion steps are not tool calls."""

    model_config = ConfigDict(extra="forbid")

    elapsed_seconds: float | None = None
    time_limit_seconds: float | None = None
    recursion_steps: int | None = None
    recursion_limit: int | None = None


class ToolRuntimeNotice(BaseModel):
    """Runtime-owned guidance, separate from tool business facts and errors."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["budget_warning", "search_no_progress", "same_sampling_new_question"] = (
        "budget_warning"
    )
    triggered_by: list[Literal["recursion_steps", "elapsed_time"]] = Field(default_factory=list)
    usage: ToolBudgetUsage = Field(default_factory=ToolBudgetUsage)
    message: str


class ToolResult(BaseModel):
    """Canonical JSON envelope returned to the agent and adapters."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    status: ToolStatus
    code: ToolErrorCode = ToolErrorCode.OK
    message: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolError | None = None
    next_action: dict[str, Any] | None = None
    runtime_notices: list[ToolRuntimeNotice] = Field(default_factory=list)

    @classmethod
    def ok(
        cls,
        tool: str,
        data: dict[str, Any] | None = None,
        *,
        message: str | None = None,
        next_action: dict[str, Any] | None = None,
    ) -> ToolResult:
        return cls(
            tool=tool,
            status=ToolStatus.OK,
            code=ToolErrorCode.OK,
            message=message,
            data=data or {},
            next_action=next_action,
        )

    @classmethod
    def fail(
        cls,
        tool: str,
        code: ToolErrorCode | str,
        message: str,
        *,
        detail: str | None = None,
        retryable: bool = False,
        next_action: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> ToolResult:
        mapped = map_error_code(code)
        return cls(
            tool=tool,
            status=ToolStatus.ERROR,
            code=mapped,
            message=message,
            data=data or {},
            error=ToolError(
                code=mapped,
                message=message,
                detail=detail,
                retryable=retryable,
            ),
            next_action=next_action,
        )

    def to_json(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        if not self.runtime_notices:
            payload.pop("runtime_notices", None)
        if self.status is ToolStatus.ERROR and not self.data:
            payload.pop("data", None)
        return json.dumps(payload, ensure_ascii=False)


def map_error_code(code: ToolErrorCode | str | None) -> ToolErrorCode:
    if isinstance(code, ToolErrorCode):
        return code
    raw = str(code or "").strip()
    if not raw:
        return ToolErrorCode.INTERNAL_ERROR
    if raw in ToolErrorCode._value2member_map_:
        return ToolErrorCode(raw)
    return _ERROR_CODE_ALIASES.get(raw, ToolErrorCode.INTERNAL_ERROR)


def infer_error_code(message: str) -> ToolErrorCode:
    text = (message or "").lower()
    if "required" in text or "invalid arguments" in text:
        return ToolErrorCode.INVALID_ARGUMENTS
    if "unknown knowledge library" in text:
        return ToolErrorCode.UNKNOWN_LIBRARY
    if "not configured" in text:
        return ToolErrorCode.NOT_CONFIGURED
    if "precompiled" in text:
        return ToolErrorCode.UNSUPPORTED
    if "timed out" in text or "timeout" in text:
        return ToolErrorCode.TIMEOUT
    return ToolErrorCode.UPSTREAM_ERROR


def unwrap_tool_data(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Expose business fields from ``data`` or a legacy top-level payload."""

    if not isinstance(payload, dict):
        return {}
    inner = payload.get("data")
    if isinstance(inner, dict) and inner:
        return {**payload, **inner}
    return payload


def nested_tool_failure(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Inspect the explicit media-result success flag; no error-text inference."""
    inner = unwrap_tool_data(payload).get("result")
    if not isinstance(inner, dict) or inner.get("success") is not False:
        return None
    error = inner.get("error")
    metadata = error if isinstance(error, dict) else inner
    retryable = metadata.get("retryable")
    return {
        "code": str(metadata.get("code") or "tool_execution_error"),
        "retryable": retryable if isinstance(retryable, bool) else None,
    }


def parse_tool_payload(raw: Any) -> dict[str, Any]:
    """Parse a tool JSON string or dict and unwrap the envelope."""

    if isinstance(raw, dict):
        return unwrap_tool_data(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return unwrap_tool_data(parsed) if isinstance(parsed, dict) else {}


def coerce_tool_result(tool: str, raw: Any) -> ToolResult:
    """Normalize handler output into the canonical envelope."""

    if isinstance(raw, ToolResult):
        return raw
    if raw is None:
        return ToolResult.fail(tool, ToolErrorCode.INTERNAL_ERROR, "empty tool result")
    payload: Any = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return ToolResult.fail(
                tool,
                ToolErrorCode.UPSTREAM_ERROR,
                "non-JSON tool result",
                detail=raw[:2000],
            )
    if not isinstance(payload, dict):
        return ToolResult.ok(tool, {"value": payload})

    name = str(payload.get("tool") or tool)
    next_action = (
        payload.get("next_action") if isinstance(payload.get("next_action"), dict) else None
    )

    if payload.get("status") in {ToolStatus.OK, ToolStatus.ERROR, "ok", "error"} and payload.get(
        "code"
    ):
        if str(payload.get("status")) == ToolStatus.ERROR:
            err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            return ToolResult.fail(
                name,
                payload.get("code") or err.get("code"),
                str(err.get("message") or payload.get("message") or ""),
                detail=err.get("detail"),
                retryable=bool(err.get("retryable")),
                next_action=next_action,
                data=payload.get("data") if isinstance(payload.get("data"), dict) else {},
            ).model_copy(
                update={
                    "runtime_notices": [
                        ToolRuntimeNotice.model_validate(item)
                        for item in payload.get("runtime_notices", [])
                    ]
                }
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            data = {
                key: value
                for key, value in payload.items()
                if key
                not in {
                    "tool",
                    "status",
                    "code",
                    "message",
                    "error",
                    "next_action",
                    "runtime_notices",
                }
            }
        return ToolResult.ok(
            name,
            data,
            message=payload.get("message") if isinstance(payload.get("message"), str) else None,
            next_action=next_action,
        ).model_copy(
            update={
                "runtime_notices": [
                    ToolRuntimeNotice.model_validate(item)
                    for item in payload.get("runtime_notices", [])
                ]
            }
        )

    if payload.get("status") == "error" and payload.get("error_code"):
        message = str(payload.get("message") or payload.get("error_code"))
        extra = {
            key: value
            for key, value in payload.items()
            if key not in {"tool", "status", "error_code", "message", "error", "next_action"}
        }
        return ToolResult.fail(
            name,
            payload.get("error_code"),
            message,
            detail=message,
            next_action=next_action,
            data=extra,
        )

    error = payload.get("error")
    if isinstance(error, dict) and (error.get("code") or error.get("message")):
        extra = {
            key: value
            for key, value in payload.items()
            if key not in {"tool", "error", "next_action"}
        }
        return ToolResult.fail(
            name,
            error.get("code") or infer_error_code(str(error.get("message") or "")),
            str(error.get("message") or ""),
            detail=error.get("detail"),
            retryable=bool(error.get("retryable")),
            next_action=next_action,
            data=extra,
        )
    if isinstance(error, str) and error:
        extra = {
            key: value
            for key, value in payload.items()
            if key not in {"tool", "error", "next_action"}
        }
        return ToolResult.fail(
            name,
            infer_error_code(error),
            error,
            detail=error,
            next_action=next_action,
            data=extra,
        )

    data = {key: value for key, value in payload.items() if key != "tool"}
    return ToolResult.ok(name, data, next_action=next_action)


class BaseTool(ABC):
    """ABC every ViKnow tool should inherit."""

    id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    execution_timeout_seconds: float | None = None
    resource_class: str | None = None

    @abstractmethod
    def run(self, args: dict[str, Any]) -> ToolResult:
        """Execute the tool and return a structured envelope."""

    async def arun(self, args: dict[str, Any]) -> ToolResult:
        return await run_tool_sync(self.run, args)

    def validate_input(self, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        return dict(args)

    def invoke(self, args: dict[str, Any], **_: Any) -> str:
        try:
            if not isinstance(args, dict):
                return ToolResult.fail(
                    self.name,
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"{self.name} received invalid arguments",
                    detail="arguments must be an object",
                ).to_json()
            result = coerce_tool_result(self.name, self.run(self.validate_input(args)))
        except Exception as exc:
            result = ToolResult.fail(
                self.name,
                ToolErrorCode.INTERNAL_ERROR,
                str(exc),
                detail=str(exc),
            )
        return result.to_json()

    async def ainvoke(self, args: dict[str, Any], **_: Any) -> str:
        try:
            if not isinstance(args, dict):
                return ToolResult.fail(
                    self.name,
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"{self.name} received invalid arguments",
                    detail="arguments must be an object",
                ).to_json()
            result = coerce_tool_result(self.name, await self.arun(self.validate_input(args)))
        except Exception as exc:
            result = ToolResult.fail(
                self.name,
                ToolErrorCode.INTERNAL_ERROR,
                str(exc),
                detail=str(exc),
            )
        return result.to_json()

    def spec(self) -> ToolSpec:
        return ToolSpec(
            id=self.id,
            name=self.name,
            schema=self.input_schema,
            handler=self.invoke,
            description=self.description,
            async_handler=self.ainvoke,
            execution_timeout_seconds=self.execution_timeout_seconds,
            resource_class=self.resource_class,
        )


class FunctionTool(BaseTool):
    """Adapter that turns an existing ``handler(args) -> str|ToolResult`` into a BaseTool."""

    def __init__(
        self,
        *,
        id: str,
        name: str,
        description: str,
        schema: dict[str, Any],
        handler: Callable[[dict[str, Any]], Any],
        async_handler: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
        execution_timeout_seconds: float | None = None,
        resource_class: str | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.input_schema = schema
        self.execution_timeout_seconds = execution_timeout_seconds
        self.resource_class = resource_class
        self._handler = handler
        self._async_handler = async_handler

    def run(self, args: dict[str, Any]) -> ToolResult:
        return coerce_tool_result(self.name, self._handler(args))

    async def arun(self, args: dict[str, Any]) -> ToolResult:
        if self._async_handler is None:
            return await run_tool_sync(self.run, args)
        return coerce_tool_result(self.name, await self._async_handler(args))


_LANGFUSE_ERROR_MESSAGE_MAX = 240
_LANGFUSE_DATA_KEY_MAX = 24


def summarize_tool_result(tool: str, raw: Any) -> dict[str, Any]:
    """Compact ToolResult fields for Langfuse metadata (no evidence dump)."""

    result = coerce_tool_result(tool, raw)
    summary: dict[str, Any] = {
        "tool": result.tool,
        "status": str(result.status),
        "code": str(result.code),
    }
    if result.error is not None:
        message = result.error.message or ""
        if len(message) > _LANGFUSE_ERROR_MESSAGE_MAX:
            message = message[:_LANGFUSE_ERROR_MESSAGE_MAX]
        summary["error"] = {
            "code": str(result.error.code),
            "message": message,
        }
        summary["retryable"] = bool(result.error.retryable)
    data = result.data if isinstance(result.data, dict) else {}
    if data:
        summary["data_keys"] = list(data.keys())[:_LANGFUSE_DATA_KEY_MAX]
    return summary
