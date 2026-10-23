"""Structured semantic diagnosis for one optimization proposal."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from viknow.core.learning.case_review import (
    OptimizationProposalAnalysis,
    OptimizationProposalAnalysisInput,
)


class ConfiguredOptimizationProposalAnalyzer:
    def __init__(self, *, chat_model: Any, model: str, max_tokens: int) -> None:
        self._chat_model = chat_model
        self.model = model
        self._max_tokens = max_tokens

    def analyze(
        self, analysis_input: OptimizationProposalAnalysisInput
    ) -> OptimizationProposalAnalysis:
        from viknow.core.multimodal.structured_llm import invoke_structured_llm

        result = invoke_structured_llm(
            self._chat_model,
            OptimizationProposalAnalysis,
            _prompt(analysis_input),
            run_name="optimization_proposal_analysis",
            max_attempts=1,
            max_tokens=self._max_tokens,
        )
        if result is None:
            raise ValueError("optimization proposal analyzer returned no valid result")
        return result


def build_optimization_proposal_analyzer(
    model_config: Any,
) -> LazyOptimizationProposalAnalyzer:
    return LazyOptimizationProposalAnalyzer(model_config)


def build_run_trace_proposal_analyzer(
    app_config: Any,
    tool_configs: Any,
) -> Any:
    """Build the internal DeepAgents analyst while keeping it outside public profiles."""

    from viknow.core.config import AgentModelConfig
    from viknow.core.learning.run_trace_analyzer import RunTraceProposalAnalyzer
    from viknow.core.observability.clickhouse_trace_reader import (
        ClickHouseTraceReader,
        ClickHouseTraceReaderConfig,
    )
    from viknow.core.runtime.builder import build_agent_runtime
    from viknow.core.runtime.events import InMemoryAgentEventStore

    model_config = app_config.self_learning_analysis.model
    agent_model = AgentModelConfig.model_validate(model_config.model_dump(exclude={"max_tokens"}))
    agent_model = agent_model.model_copy(
        update={"enable_thinking": app_config.self_learning_analysis.thinking_enabled}
    )
    if agent_model.max_input_tokens is None:
        agent_model = agent_model.model_copy(
            update={"max_input_tokens": app_config.agent.model.max_input_tokens}
        )
    analyst_sandbox = app_config.agent.deepagents.sandbox.model_copy(
        update={"network_access": False, "extra_bind_mounts": [], "extra_env": {}}
    )
    analyst_deepagents = app_config.agent.deepagents.model_copy(update={"sandbox": analyst_sandbox})
    runtime_config = app_config.model_copy(
        update={
            "agent": app_config.agent.model_copy(
                update={"model": agent_model, "deepagents": analyst_deepagents}
            )
        }
    )

    def runtime_factory(profile, tool_specs):
        return build_agent_runtime(
            runtime_config,
            profile=profile,
            tool_configs=tool_configs,
            workspace_root=Path.cwd(),
            event_store=InMemoryAgentEventStore(),
            extra_tool_specs=tool_specs,
            multi_agent_enabled=False,
        )

    reader = None
    clickhouse_url = os.getenv("CLICKHOUSE_URL")
    clickhouse_user = os.getenv("CLICKHOUSE_USER")
    clickhouse_password = os.getenv("CLICKHOUSE_PASSWORD")
    if clickhouse_url and clickhouse_user and clickhouse_password is not None:
        reader = ClickHouseTraceReader(
            ClickHouseTraceReaderConfig(
                url=clickhouse_url,
                user=clickhouse_user,
                password=clickhouse_password,
            )
        )
    return RunTraceProposalAnalyzer(
        runtime_factory=runtime_factory,
        reader=reader,
        model=agent_model.selection_id() or agent_model.require_model(),
    )


class LazyOptimizationProposalAnalyzer:
    """Delay credential and client resolution until a proposal actually runs."""

    def __init__(self, model_config: Any) -> None:
        self._model_config = model_config
        self.model = (
            model_config.model
            or (os.getenv(model_config.model_env) if model_config.model_env else None)
            or "unconfigured"
        )

    def analyze(
        self, analysis_input: OptimizationProposalAnalysisInput
    ) -> OptimizationProposalAnalysis:
        analyzer = _build_configured_analyzer(self._model_config)
        self.model = analyzer.model
        return analyzer.analyze(analysis_input)


def _build_configured_analyzer(model_config: Any) -> ConfiguredOptimizationProposalAnalyzer:
    from langchain_openai import ChatOpenAI

    from viknow.core.config import ModelProvider

    if model_config.provider != ModelProvider.OPENAI_COMPATIBLE:
        raise ValueError(f"unsupported proposal analysis provider: {model_config.provider}")
    base_url = model_config.resolve_base_url()
    if not base_url:
        raise RuntimeError("proposal analysis base_url or base_url_env is required")
    model = model_config.require_model()
    return ConfiguredOptimizationProposalAnalyzer(
        chat_model=ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=model_config.require_api_key(),
            temperature=model_config.temperature,
            timeout=model_config.timeout_seconds,
            max_retries=0,
        ),
        model=model,
        max_tokens=model_config.max_tokens,
    )


def _prompt(analysis_input: OptimizationProposalAnalysisInput) -> str:
    payload = json.dumps(analysis_input.model_dump(mode="json"), ensure_ascii=False)
    return f"""你是 ViKnow 自学习系统的语义归因器。规则层已经确定 bucket、risk 和
reason_codes；不得改写或质疑这些事实。根据受限输入判断问题应由哪个方向处理，并给出
简短、可实验验证的优化建议。

handling_direction 只能是：skill_instruction、harness_policy、tool_or_index、
knowledge_source、model_behavior、product_ui、no_action、uncertain。
只有 handling_direction=skill_instruction 时才允许填写 target_skill_id；其余情况必须为 null。
requires_oracle 是一个保留字段，true 表示建议进行素材研究，不是进入优化的前置门禁；如果为
true，必须填写 oracle_reason，说明补充参考答案或证据为何有帮助。如果已有 trace 足以支持
优化（例如回答结构、工具调用失败或 UI 问题），应为 false 且 oracle_reason=null。
信息不足时选择 uncertain，不要虚构 trace、工具输出或知识内容。

输入：{payload}
"""
