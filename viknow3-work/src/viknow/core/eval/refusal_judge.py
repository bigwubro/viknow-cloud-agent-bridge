"""Semantic fallback for traces without an agent-authored answer disposition."""

from typing import Any

from viknow.core.eval.skill_flow import (
    RefusalJudgeDecision,
    RefusalJudgeRequest,
)


class ConfiguredRefusalJudge:
    def __init__(self, *, chat_model: Any, max_tokens: int) -> None:
        self._chat_model = chat_model
        self._max_tokens = max_tokens

    def judge_refusal(self, request: RefusalJudgeRequest) -> RefusalJudgeDecision:
        from viknow.core.multimodal.structured_llm import invoke_structured_llm

        result = invoke_structured_llm(
            self._chat_model,
            RefusalJudgeDecision,
            (
                "Determine whether the assistant's final response declines to answer the "
                "requested fact because the available source evidence is insufficient. "
                "A partial, approximate, conditional, or alternative concrete answer is not "
                "a refusal. Judge semantics, not phrases.\n\n"
                f"Question:\n{request.question}\n\nFinal response:\n{request.answer}"
            ),
            run_name="skill_flow_refusal_judge",
            max_attempts=1,
            max_tokens=self._max_tokens,
        )
        if result is None:
            raise ValueError("refusal judge returned no valid structured result")
        return result


def build_refusal_judge(model_config: Any) -> ConfiguredRefusalJudge:
    from langchain_openai import ChatOpenAI

    from viknow.core.config import ModelProvider

    if model_config.provider != ModelProvider.OPENAI_COMPATIBLE:
        raise ValueError(f"unsupported refusal judge provider: {model_config.provider}")
    base_url = model_config.resolve_base_url()
    if not base_url:
        raise RuntimeError("refusal judge base_url or base_url_env is required")
    return ConfiguredRefusalJudge(
        chat_model=ChatOpenAI(
            model=model_config.require_model(),
            base_url=base_url,
            api_key=model_config.require_api_key(),
            temperature=model_config.temperature,
            timeout=model_config.timeout_seconds,
            max_retries=0,
        ),
        max_tokens=model_config.max_tokens,
    )
