"""Agents delivered from outside qa_expert.

A LangGraph graph is run directly by agentcore. deepagents hands over the graph
from create_deep_agent. ADK runs in-process via agentcore.runtime.adk_runner.
"""

from __future__ import annotations

from typing import Any


def langgraph_available() -> bool:
    try:
        import langgraph.graph  # noqa: F401
    except ImportError:
        return False
    return True


def echo_graph(name: str, checkpointer: Any) -> Any:
    """Replies 「收到：<message>」 through a chat model node, so tokens stream like a real model."""
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, AIMessageChunk
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
    from langchain_core.runnables import RunnableConfig
    from langgraph.graph import END, START, MessagesState, StateGraph

    class _EchoModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "viknow-echo"

        def _generate(self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
            text = f"收到：{messages[-1].content}"
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

        def _stream(self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any):
            yield ChatGenerationChunk(message=AIMessageChunk(content=f"收到：{messages[-1].content}"))

    model = _EchoModel()

    async def reply(state: dict, config: RunnableConfig) -> dict:
        return {"messages": [await model.ainvoke(state["messages"], config)]}

    graph = StateGraph(MessagesState)
    graph.add_node("reply", reply)
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    return graph.compile(checkpointer=checkpointer, name=name)


def adk_in_process_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("google.adk") is not None
    except Exception:
        return False
