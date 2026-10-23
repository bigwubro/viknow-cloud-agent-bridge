"""Per-build QA middleware selection for the installed DeepAgents factory."""

from types import FunctionType
from typing import Any

from langchain.agents.middleware import TodoListMiddleware


def create_qa_deep_agent(**kwargs: Any) -> Any:
    """Keep upstream graph construction, excluding only the main QA Todo owner.

    DeepAgents 0.5.3 only exposes model-global harness profiles. Rebind its final
    constructor in a private namespace instead: neither the model registry nor
    another profile's construction is changed. Subagent construction uses its
    own module and is intentionally unaffected. Remove this adapter when an
    upstream per-call middleware exclusion parameter is available.
    """
    from deepagents import create_deep_agent

    upstream_create_agent = create_deep_agent.__globals__["create_agent"]

    def create_main_agent(*args: Any, **options: Any) -> Any:
        options["middleware"] = [
            middleware
            for middleware in options["middleware"]
            if not isinstance(middleware, TodoListMiddleware)
        ]
        return upstream_create_agent(*args, **options)

    factory = FunctionType(
        create_deep_agent.__code__,
        {**create_deep_agent.__globals__, "create_agent": create_main_agent},
        create_deep_agent.__name__,
        create_deep_agent.__defaults__,
        create_deep_agent.__closure__,
    )
    factory.__kwdefaults__ = create_deep_agent.__kwdefaults__
    return factory(**kwargs)
