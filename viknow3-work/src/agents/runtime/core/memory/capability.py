"""System-prompt notices describing ViKnow memory capability for the current user."""

from __future__ import annotations

_MEMORY_CAPABILITY_ENABLED = """\
## 记忆能力（当前用户已开启）

ViKnow 为本用户启用了长期记忆系统：
- 对话开始前，系统会注入「用户偏好」「用户背景」「相关经历」等记忆块（若存在相关内容）。
- 本轮对话成功结束后，系统会在后台从对话中自动抽取并保存可长期复用的信息；
  你没有单独的“写入记忆”工具。
- 当用户要求你“记住”某事时：可以说明本用户已开启记忆，系统会尝试在对话结束后自动留存，
  但无法保证每一条都会被完整记住或在下轮准确召回；不要承诺“我一定记住了”。
- 用户可在设置中直接配置偏好（人设、回答风格等），这类设置优先级高于从对话推断的偏好。"""

_MEMORY_CAPABILITY_DISABLED = """\
## 记忆能力（当前用户未开启）

ViKnow 长期记忆系统对本用户**未开启**：
- 你无法跨会话保留用户信息；仅能利用本轮对话与当前会话上下文。
- 当用户要求你“记住”某事时：必须如实说明当前无法长期记住，并建议用户在 ViKnow 设置中开启“记忆”功能。
- 不要假装已记住，不要承诺下次对话仍能回忆。"""


def build_memory_capability_notice(*, memory_enabled: bool) -> str:
    """Return USER-segment instructions about memory for the current run."""
    if memory_enabled:
        return _MEMORY_CAPABILITY_ENABLED
    return _MEMORY_CAPABILITY_DISABLED
