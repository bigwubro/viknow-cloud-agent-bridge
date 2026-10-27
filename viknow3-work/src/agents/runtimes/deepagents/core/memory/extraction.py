"""Per-kind mem0 extraction prompts for conversation sync."""

from __future__ import annotations

from agents.runtimes.deepagents.core.memory.domain import MemoryKind

KIND_EXTRACTION_PROMPTS: dict[MemoryKind, str] = {
    MemoryKind.USER_PREFERENCE: """\
仅抽取用户明确表达的交互偏好：回答风格、语言、人设期待等。
不要抽取职业事实、项目事实、单次对话事件或 Agent 提供的信息。
若无偏好信号，返回空。""",
    MemoryKind.FACT: """\
仅抽取关于用户或其上下文的稳定事实：职业、技术栈、项目、关系等。
不要抽取回答风格、人设期待或本次对话过程摘要。
若无稳定事实，返回空。""",
    MemoryKind.EPISODIC: """\
从本轮 user 与 assistant 的完整交互中，抽取本次实例化事件：
用户意图、Agent 采取的行动或给出的结论（situation / action / outcome）。
必须保留 session 或时间语境。不要抽取长期人设、稳定画像或回答风格偏好。
若无值得记录的事件，返回空。""",
}
