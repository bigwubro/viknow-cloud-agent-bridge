"""Hermes kernel for ViKnow Agent Runtime."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from agents.runtime.core.config import (
    AgentModelConfig,
    ModelProvider,
    ModelProviderConfig,
    resolve_model_config,
)
from agents.runtime.core.runtime.domain import (
    AgentEvent,
    AgentEventType,
    AgentOp,
    AgentOpType,
    AgentProfile,
    AgentStatus,
    AgentStatusType,
    SubmitResult,
    ToolCallRecord,
)
from agents.runtime.core.runtime.events import (
    AgentEventMapper,
    AgentEventStore,
    AgentRuntime,
    InMemoryAgentEventStore,
)

HERMES_PLUGIN_SOURCE_ROOT = Path(__file__).resolve().parent / "hermes_plugins"
VIKNOW_MANAGED_PLUGIN_MARKER = ".viknow-managed"
_HERMES_TOOL_COMPAT: dict[str, tuple[str, str]] = {
    "viknow_test_answer": ("viknow-test", "viknow_test"),
    "knowledge_discover": ("viknow-knowledge-search", "viknow_search"),
    "knowledge_search": ("viknow-knowledge-search", "viknow_search"),
    "video_inspect": ("viknow-evidence-reviewer", "viknow_multimodal"),
    "evidence_asr": ("viknow-evidence-reviewer", "viknow_multimodal"),
    "evidence_ocr": ("viknow-evidence-reviewer", "viknow_multimodal"),
    "clip_search": ("viknow-evidence-reviewer", "viknow_multimodal"),
    "global_browse": ("viknow-evidence-reviewer", "viknow_multimodal"),
    "frame_inspect": ("viknow-evidence-reviewer", "viknow_multimodal"),
}
HERMES_COMPATIBLE_TOOL_IDS = frozenset(_HERMES_TOOL_COMPAT)


@dataclass
class _RunState:
    run_id: str
    session_id: str | None
    status: AgentStatusType
    queue: asyncio.Queue[AgentEvent] = field(default_factory=asyncio.Queue)
    sequence: int = 0
    message: str | None = None
    task: asyncio.Task[None] | None = None
    streamed_text: str = ""
    completed_message_emitted: bool = False
    started_tool_call_ids: set[str] = field(default_factory=set)
    completed_tool_call_ids: set[str] = field(default_factory=set)


class HermesEventMapper(AgentEventMapper):
    """Map Hermes callbacks and raw run results into ViKnow runtime events."""

    def map_stream_delta(self, delta: Any) -> tuple[AgentEventType, dict[str, Any]] | None:
        if not delta:
            return None
        return AgentEventType.MESSAGE_DELTA, {"text": str(delta)}

    def map_tool_call_started(
        self, call_id: str, name: str, arguments: Any
    ) -> tuple[AgentEventType, dict[str, Any]]:
        return (
            AgentEventType.TOOL_CALL_STARTED,
            {
                "id": call_id,
                "name": name,
                "arguments": self.serialize_tool_value(arguments),
            },
        )

    def map_tool_call_completed(
        self, call_id: str, name: str, arguments: Any, result: Any
    ) -> tuple[AgentEventType, dict[str, Any]]:
        return (
            AgentEventType.TOOL_CALL_COMPLETED,
            {
                "id": call_id,
                "name": name,
                "arguments": self.serialize_tool_value(arguments),
                "result": self.serialize_tool_value(result),
            },
        )

    def map_final_answer(self, result: Any) -> str:
        if isinstance(result, dict):
            value = result.get("final_response") or result.get("answer") or result.get("content")
            if value is not None:
                return str(value)
        if isinstance(result, str):
            return result
        return str(result)

    def map_final_reasoning(self, result: Any) -> str:
        return ""

    def map_final_tool_calls(self, result: Any) -> list[ToolCallRecord]:
        if not isinstance(result, dict):
            return []
        messages = result.get("messages")
        if not isinstance(messages, list):
            return []

        calls: dict[str, ToolCallRecord] = {}
        order: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            for tool_call in message.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                call_id = str(tool_call.get("id") or tool_call.get("call_id") or "")
                if not call_id:
                    continue
                calls[call_id] = ToolCallRecord(
                    id=call_id,
                    name=str(function.get("name") or ""),
                    arguments=str(function.get("arguments") or ""),
                )
                order.append(call_id)

            if message.get("role") == "tool":
                call_id = str(message.get("tool_call_id") or "")
                if call_id and call_id in calls:
                    calls[call_id].result = str(message.get("content") or "")

        return [calls[call_id] for call_id in order if call_id in calls]

    def serialize_tool_value(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)


class HermesAgentRuntime(AgentRuntime):
    """Run ViKnow chat turns through Hermes AIAgent."""

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        hermes_home: Path | None = None,
        agent_factory: Any | None = None,
        profile: AgentProfile | None = None,
        model_config: AgentModelConfig | None = None,
        model_providers: dict[str, ModelProviderConfig] | None = None,
        event_mapper: AgentEventMapper | None = None,
        event_store: AgentEventStore | None = None,
    ) -> None:
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.hermes_home = (
            hermes_home or self.workspace_root / ".viknow" / "hermes-home"
        ).resolve()
        self._agent_factory = agent_factory
        self.profile = profile or AgentProfile(
            id="default",
            label="Default",
            description="Default Hermes runtime profile",
        )
        self.model_config = model_config
        self.model_providers = dict(model_providers or {})
        self.event_mapper = event_mapper or HermesEventMapper()
        self.event_store = event_store or InMemoryAgentEventStore()
        self._runs: dict[str, _RunState] = {}
        self._shutdown = False

    async def submit(self, op: AgentOp) -> SubmitResult:
        if self._shutdown:
            raise RuntimeError("agent runtime is shut down")
        if op.type == AgentOpType.USER_INPUT:
            if not op.input:
                raise ValueError("user_input operation requires input")
            run_id = op.run_id or str(uuid4())
            state = _RunState(
                run_id=run_id,
                session_id=op.session_id,
                status=AgentStatusType.RUNNING,
            )
            self._runs[run_id] = state
            state.task = asyncio.create_task(self._execute_user_input(op, state))
            return SubmitResult(run_id=run_id, session_id=op.session_id)
        if op.type == AgentOpType.INTERRUPT:
            if not op.run_id:
                raise ValueError("interrupt operation requires run_id")
            state = self._runs.get(op.run_id)
            if state is None:
                raise KeyError(op.run_id)
            state.status = AgentStatusType.INTERRUPTED
            await self._emit_streamed_snapshots(state)
            await self._emit(
                state,
                AgentEventType.RUN_INTERRUPTED,
                {"message": "run interrupted"},
            )
            return SubmitResult(run_id=op.run_id, session_id=state.session_id)
        if op.type == AgentOpType.SHUTDOWN:
            await self.aclose()
            run_id = op.run_id or str(uuid4())
            return SubmitResult(run_id=run_id, session_id=op.session_id)
        raise ValueError(f"unsupported agent operation: {op.type}")

    async def aclose(self) -> None:
        """Mark runtime shut down. Idempotent."""

        self._shutdown = True

    async def next_event(self, run_id: str | None = None) -> AgentEvent:
        state = self._resolve_run(run_id)
        return await state.queue.get()

    async def events_after(self, run_id: str, after_sequence: int = 0) -> list[AgentEvent]:
        self._resolve_run(run_id)
        return self.event_store.events_after(run_id, after_sequence)

    async def get_status(self, run_id: str | None = None) -> AgentStatus:
        if run_id is None:
            if self._shutdown:
                return AgentStatus(status=AgentStatusType.SHUTDOWN)
            active = [
                state for state in self._runs.values() if state.status == AgentStatusType.RUNNING
            ]
            if not active:
                return AgentStatus(status=AgentStatusType.IDLE)
            return AgentStatus(
                status=AgentStatusType.RUNNING,
                run_id=active[-1].run_id,
                session_id=active[-1].session_id,
            )
        state = self._runs.get(run_id)
        if state is None:
            raise KeyError(run_id)
        return AgentStatus(
            status=state.status,
            run_id=state.run_id,
            session_id=state.session_id,
            message=state.message,
        )

    async def _execute_user_input(self, op: AgentOp, state: _RunState) -> None:
        await self._emit(
            state,
            AgentEventType.RUN_STARTED,
            {
                "session_id": op.session_id,
                "enabled_tools": list(self.profile.enabled_tools),
                "enabled_skills": list(self.profile.enabled_skills),
            },
        )
        await self._emit(
            state,
            AgentEventType.USER_MESSAGE,
            {
                "text": op.input or "",
                "session_id": state.session_id,
                "user_id": op.user_id,
                "workspace_id": op.workspace_id,
            },
        )
        loop = asyncio.get_running_loop()
        try:
            result, session_id = await asyncio.to_thread(
                self._run_hermes_conversation,
                op,
                state.run_id,
                state,
                loop,
            )
            await asyncio.sleep(0)
        except Exception as exc:
            state.status = AgentStatusType.FAILED
            state.message = str(exc)
            await self._emit_streamed_snapshots(state)
            await self._emit(
                state,
                AgentEventType.RUN_FAILED,
                {"message": str(exc)},
            )
            return

        if state.status == AgentStatusType.INTERRUPTED:
            return

        state.session_id = session_id
        raw = result if isinstance(result, dict) else None
        for tool_call in self.event_mapper.map_final_tool_calls(raw):
            if tool_call.id in state.started_tool_call_ids:
                continue
            await self._emit(
                state,
                AgentEventType.TOOL_CALL_STARTED,
                {
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                },
            )
            state.started_tool_call_ids.add(tool_call.id)

        for tool_call in self.event_mapper.map_final_tool_calls(raw):
            if tool_call.id in state.completed_tool_call_ids:
                continue
            await self._emit(
                state,
                AgentEventType.TOOL_CALL_COMPLETED,
                {
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                    "result": tool_call.result,
                },
            )
            state.completed_tool_call_ids.add(tool_call.id)

        answer = self.event_mapper.map_final_answer(result)
        if not state.streamed_text and answer:
            await self._emit(state, AgentEventType.MESSAGE_DELTA, {"text": answer})
            state.streamed_text += answer
        await self._emit(state, AgentEventType.MESSAGE_COMPLETED, {"text": answer})
        state.status = AgentStatusType.COMPLETED
        await self._emit(
            state,
            AgentEventType.RUN_COMPLETED,
            {"session_id": state.session_id},
        )

    async def _emit_streamed_snapshots(self, state: _RunState) -> None:
        if state.streamed_text.strip() and not state.completed_message_emitted:
            await self._emit(
                state,
                AgentEventType.MESSAGE_COMPLETED,
                {"text": state.streamed_text},
            )

    def _run_hermes_conversation(
        self,
        op: AgentOp,
        run_id: str,
        state: _RunState,
        loop: asyncio.AbstractEventLoop,
    ) -> tuple[Any, str | None]:
        self._ensure_hermes_home()
        prompt = self._build_prompt(op.input or "")

        environment = self._hermes_environment() if self._agent_factory is None else nullcontext()
        with environment:
            self._discover_plugins()
            agent = self._create_agent(op, state=state, loop=loop)
            result = agent.run_conversation(
                prompt,
                task_id=run_id,
                stream_callback=self._stream_delta_callback(state, loop),
            )

        session_id = getattr(agent, "session_id", None) or op.session_id
        return result, session_id

    def _create_agent(
        self,
        op: AgentOp,
        *,
        state: _RunState,
        loop: asyncio.AbstractEventLoop,
    ) -> Any:
        factory = self._agent_factory or _load_hermes_agent_factory()
        kwargs: dict[str, Any] = {
            "quiet_mode": True,
            "skip_memory": True,
            "enabled_toolsets": _hermes_toolsets_for_tools(self.profile.enabled_tools),
            "session_id": op.session_id,
            "platform": "viknow",
            "user_id": op.user_id,
            "chat_id": op.workspace_id,
            "skip_context_files": False,
            "tool_start_callback": self._tool_start_callback(state, loop),
            "tool_complete_callback": self._tool_complete_callback(state, loop),
        }
        system_prompt = self._build_profile_system_prompt()
        if system_prompt:
            kwargs["ephemeral_system_prompt"] = system_prompt
        kwargs.update(_agent_config_from_hermes_home(self.hermes_home))
        if self.model_config is not None:
            kwargs.update(
                _agent_config_from_model_config(
                    resolve_model_config(
                        self.model_config,
                        self.model_providers,
                        field_name="agent.model",
                    )
                )
            )
        for key, env_key in {
            "model": "VIKNOW_HERMES_MODEL",
            "provider": "VIKNOW_HERMES_PROVIDER",
            "base_url": "VIKNOW_HERMES_BASE_URL",
            "api_key": "VIKNOW_HERMES_API_KEY",
            "api_mode": "VIKNOW_HERMES_API_MODE",
        }.items():
            value = os.getenv(env_key)
            if value:
                kwargs[key] = value
        return factory(**kwargs)

    async def _emit(
        self,
        state: _RunState,
        event_type: AgentEventType,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._put_event(state, event_type, payload or {})

    def _emit_threadsafe(
        self,
        loop: asyncio.AbstractEventLoop,
        state: _RunState,
        event_type: AgentEventType,
        payload: dict[str, Any] | None = None,
    ) -> None:
        loop.call_soon_threadsafe(
            self._put_event,
            state,
            event_type,
            payload or {},
        )

    def _put_event(
        self,
        state: _RunState,
        event_type: AgentEventType,
        payload: dict[str, Any],
    ) -> None:
        state.sequence += 1
        event = AgentEvent(
            type=event_type,
            run_id=state.run_id,
            sequence=state.sequence,
            payload=payload,
        )
        self.event_store.append(event)
        state.queue.put_nowait(event)
        if event_type == AgentEventType.MESSAGE_COMPLETED:
            state.completed_message_emitted = True

    def _stream_delta_callback(
        self,
        state: _RunState,
        loop: asyncio.AbstractEventLoop,
    ):
        def callback(text: str | None) -> None:
            mapped = self.event_mapper.map_stream_delta(text)
            if mapped is None:
                return
            event_type, payload = mapped

            def emit() -> None:
                state.streamed_text += str(payload.get("text") or "")
                self._put_event(state, event_type, payload)

            loop.call_soon_threadsafe(emit)

        return callback

    def _tool_start_callback(
        self,
        state: _RunState,
        loop: asyncio.AbstractEventLoop,
    ):
        def callback(call_id: str, name: str, args: Any) -> None:
            event_type, payload = self.event_mapper.map_tool_call_started(call_id, name, args)

            def emit() -> None:
                state.started_tool_call_ids.add(call_id)
                self._put_event(state, event_type, payload)

            loop.call_soon_threadsafe(emit)

        return callback

    def _tool_complete_callback(
        self,
        state: _RunState,
        loop: asyncio.AbstractEventLoop,
    ):
        def callback(call_id: str, name: str, args: Any, result: Any) -> None:
            event_type, payload = self.event_mapper.map_tool_call_completed(
                call_id, name, args, result
            )

            def emit() -> None:
                state.completed_tool_call_ids.add(call_id)
                self._put_event(state, event_type, payload)

            loop.call_soon_threadsafe(emit)

        return callback

    def _resolve_run(self, run_id: str | None) -> _RunState:
        if run_id is None:
            if len(self._runs) != 1:
                raise ValueError("run_id is required when runtime has multiple runs")
            return next(iter(self._runs.values()))
        state = self._runs.get(run_id)
        if state is None:
            raise KeyError(run_id)
        return state

    def _ensure_hermes_home(self) -> None:
        self.hermes_home.mkdir(parents=True, exist_ok=True)
        self._sync_builtin_plugins()
        config_path = self.hermes_home / "config.yaml"
        config = _load_yaml_config(config_path)
        plugins = config.setdefault("plugins", {})
        if not isinstance(plugins, dict):
            plugins = {}
            config["plugins"] = plugins
        enabled = plugins.setdefault("enabled", [])
        if not isinstance(enabled, list):
            enabled = []
            plugins["enabled"] = enabled
        changed = False
        for plugin in _hermes_plugins_for_tools(self.profile.enabled_tools):
            if plugin not in enabled:
                enabled.append(plugin)
                changed = True
        if changed:
            _write_yaml_config(config_path, config)

        model = config.get("model")
        if isinstance(model, dict) and "name" in model and "default" not in model:
            model["default"] = model["name"]
            _write_yaml_config(config_path, config)

    def _sync_builtin_plugins(self) -> None:
        if not HERMES_PLUGIN_SOURCE_ROOT.exists():
            return

        target_root = self.hermes_home / "plugins"
        target_root.mkdir(parents=True, exist_ok=True)
        for source_dir in sorted(HERMES_PLUGIN_SOURCE_ROOT.iterdir()):
            if not source_dir.is_dir() or source_dir.name == "__pycache__":
                continue
            manifest_path = source_dir / "plugin.yaml"
            if not manifest_path.exists():
                continue

            manifest = _load_yaml_config(manifest_path)
            plugin_name = str(manifest.get("name") or source_dir.name)
            target_dir = target_root / plugin_name
            if target_dir.exists():
                marker_path = target_dir / VIKNOW_MANAGED_PLUGIN_MARKER
                if not marker_path.exists():
                    raise RuntimeError(
                        f"refusing to overwrite unmanaged Hermes plugin: {target_dir}"
                    )
                shutil.rmtree(target_dir)
            shutil.copytree(
                source_dir,
                target_dir,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            (target_dir / VIKNOW_MANAGED_PLUGIN_MARKER).write_text(
                "managed by ViKnow\n",
                encoding="utf-8",
            )

    def _build_prompt(self, question: str) -> str:
        return self.profile.user_prompt_template.format(input=question)

    def _build_profile_system_prompt(self) -> str | None:
        parts: list[str] = []
        if self.profile.enabled_skills:
            skills = ", ".join(f"`{name}`" for name in self.profile.enabled_skills)
            parts.append(
                "Before answering in this ViKnow profile, load and follow these "
                f"Hermes plugin skills with skill_view: {skills}."
            )
        if self.profile.system_prompt:
            parts.append(self.profile.system_prompt)
        return "\n\n".join(parts) or None

    def _discover_plugins(self) -> None:
        from hermes_cli.plugins import discover_plugins

        discover_plugins(force=True)

    @contextmanager
    def _hermes_environment(self):
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        previous_cwd = Path.cwd()
        token = set_hermes_home_override(self.hermes_home)
        try:
            os.chdir(self.workspace_root)
            yield
        finally:
            os.chdir(previous_cwd)
            reset_hermes_home_override(token)


def _load_hermes_agent_factory() -> Any:
    from run_agent import AIAgent

    return AIAgent


def _hermes_plugins_for_tools(enabled_tools: Sequence[str]) -> list[str]:
    plugins = []
    for tool in enabled_tools:
        plugin, _toolset = _HERMES_TOOL_COMPAT[tool]
        plugins.append(plugin)
    return _ordered_unique(plugins)


def _hermes_toolsets_for_tools(enabled_tools: Sequence[str]) -> list[str]:
    toolsets = []
    for tool in enabled_tools:
        _plugin, toolset = _HERMES_TOOL_COMPAT[tool]
        toolsets.append(toolset)
    return _ordered_unique(toolsets)


def _ordered_unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _load_yaml_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _write_yaml_config(path: Path, config: dict[str, Any]) -> None:
    import yaml

    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _agent_config_from_hermes_home(home: str | Path) -> dict[str, str]:
    config = _load_yaml_config(Path(home) / "config.yaml")
    model = config.get("model")
    if not isinstance(model, dict):
        return {}

    values = {
        "model": model.get("default") or model.get("name") or model.get("model"),
        "provider": model.get("provider"),
        "base_url": model.get("base_url"),
        "api_key": model.get("api_key"),
        "api_mode": model.get("api_mode"),
    }
    return {key: str(value) for key, value in values.items() if value}


def _agent_config_from_model_config(model_config: AgentModelConfig) -> dict[str, str]:
    if model_config.provider != ModelProvider.OPENAI_COMPATIBLE:
        raise ValueError(f"unsupported Hermes model provider: {model_config.provider}")
    values = {
        "model": model_config.require_model(),
        "api_key": model_config.require_api_key(),
        "api_mode": "chat_completions",
    }
    base_url = model_config.resolve_base_url()
    if base_url:
        values["provider"] = "custom"
        values["base_url"] = base_url
    return values
