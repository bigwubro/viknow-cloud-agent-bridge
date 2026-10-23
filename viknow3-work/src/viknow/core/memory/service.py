"""User memory service boundary."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from viknow.core.memory.domain import (
    CUSTOM_NOTE_FIELD,
    PREFERENCE_FIELD_LABELS,
    MemoryKind,
    MemoryScope,
    MemorySource,
    UserPreferences,
)
from viknow.core.memory.mem0_client import Mem0Client


@runtime_checkable
class UserMemoryService(Protocol):
    async def load_preference_context(self, *, user_id: str, query: str) -> str: ...

    async def load_memory_context(self, *, user_id: str, query: str) -> str: ...

    def schedule_sync_turn(
        self,
        *,
        user_id: str,
        user_input: str,
        assistant_answer: str,
        session_id: str | None = None,
    ) -> None: ...

    async def get_preferences(self, user_id: str) -> UserPreferences: ...

    async def save_preferences(
        self, user_id: str, preferences: UserPreferences
    ) -> UserPreferences: ...


class NoOpUserMemoryService:
    async def load_preference_context(self, *, user_id: str, query: str) -> str:
        return ""

    async def load_memory_context(self, *, user_id: str, query: str) -> str:
        return ""

    def schedule_sync_turn(
        self,
        *,
        user_id: str,
        user_input: str,
        assistant_answer: str,
        session_id: str | None = None,
    ) -> None:
        return None

    async def get_preferences(self, user_id: str) -> UserPreferences:
        return UserPreferences()

    async def save_preferences(self, user_id: str, preferences: UserPreferences) -> UserPreferences:
        return preferences


class Mem0UserMemoryService:
    """mem0-backed user memory with layered kind sync and prompt injection."""

    _EXPLICIT_FILTER = {"viknow_source": MemorySource.USER_EXPLICIT.value}
    _EXTRACTED_FILTER = {"viknow_source": MemorySource.CONVERSATION_EXTRACTED.value}

    def __init__(
        self,
        client: Mem0Client,
        *,
        extraction_prompts: dict[MemoryKind, str],
        extracted_search_top_k: int = 8,
    ) -> None:
        self._client = client
        self._extraction_prompts = extraction_prompts
        self._extracted_search_top_k = extracted_search_top_k

    async def load_preference_context(self, *, user_id: str, query: str) -> str:
        scope = MemoryScope(user_id=user_id, kind=MemoryKind.USER_PREFERENCE)
        try:
            explicit_records = await asyncio.to_thread(
                self._client.get_all,
                scope,
                extra_filters=self._EXPLICIT_FILTER,
            )
            extracted_records = await asyncio.to_thread(
                self._client.search,
                scope,
                query or "用户偏好",
                top_k=self._extracted_search_top_k,
                extra_filters=self._EXTRACTED_FILTER,
            )
        except Exception as exc:
            logger.warning("failed to load user preference context for {}: {}", user_id, exc)
            return ""
        return _format_preference_context(explicit_records, extracted_records)

    async def load_memory_context(self, *, user_id: str, query: str) -> str:
        search_query = query or "用户背景"
        try:
            fact_records = await asyncio.to_thread(
                self._client.search,
                MemoryScope(user_id=user_id, kind=MemoryKind.FACT),
                search_query,
                top_k=self._extracted_search_top_k,
                extra_filters=self._EXTRACTED_FILTER,
            )
            episodic_records = await asyncio.to_thread(
                self._client.search,
                MemoryScope(user_id=user_id, kind=MemoryKind.EPISODIC),
                search_query,
                top_k=self._extracted_search_top_k,
                extra_filters=self._EXTRACTED_FILTER,
            )
        except Exception as exc:
            logger.warning("failed to load memory context for {}: {}", user_id, exc)
            return ""
        return _format_memory_context(fact_records, episodic_records)

    def schedule_sync_turn(
        self,
        *,
        user_id: str,
        user_input: str,
        assistant_answer: str,
        session_id: str | None = None,
    ) -> None:
        normalized_input = user_input.strip()
        if not normalized_input:
            return

        sync_jobs: list[tuple[MemoryKind, list[dict[str, str]], str | None]] = [
            (
                MemoryKind.USER_PREFERENCE,
                [{"role": "user", "content": user_input}],
                None,
            ),
            (
                MemoryKind.FACT,
                [{"role": "user", "content": user_input}],
                None,
            ),
        ]
        normalized_answer = assistant_answer.strip()
        if normalized_answer:
            sync_jobs.append(
                (
                    MemoryKind.EPISODIC,
                    [
                        {"role": "user", "content": user_input},
                        {"role": "assistant", "content": assistant_answer},
                    ],
                    session_id,
                )
            )

        metadata = {"viknow_source": MemorySource.CONVERSATION_EXTRACTED.value}

        async def _run() -> None:
            for kind, messages, episodic_session_id in sync_jobs:
                scope = MemoryScope(
                    user_id=user_id,
                    kind=kind,
                    session_id=episodic_session_id,
                )
                try:
                    await asyncio.to_thread(
                        self._client.add,
                        scope,
                        messages,
                        infer=True,
                        metadata=metadata,
                        prompt=self._extraction_prompts[kind],
                    )
                except Exception as exc:
                    logger.warning(
                        "failed to sync {} memory for {}: {}",
                        kind.value,
                        user_id,
                        exc,
                    )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_run())

    async def get_preferences(self, user_id: str) -> UserPreferences:
        scope = MemoryScope(user_id=user_id, kind=MemoryKind.USER_PREFERENCE)
        records = await asyncio.to_thread(
            self._client.get_all,
            scope,
            extra_filters=self._EXPLICIT_FILTER,
        )
        return _preferences_from_records(records)

    async def save_preferences(self, user_id: str, preferences: UserPreferences) -> UserPreferences:
        scope = MemoryScope(user_id=user_id, kind=MemoryKind.USER_PREFERENCE)
        await asyncio.to_thread(self._sync_scalar_field, scope, "persona", preferences.persona)
        await asyncio.to_thread(
            self._sync_scalar_field, scope, "occupation", preferences.occupation
        )
        await asyncio.to_thread(
            self._sync_scalar_field, scope, "answer_style", preferences.answer_style
        )
        await asyncio.to_thread(self._sync_custom_notes, scope, preferences.custom_notes)
        return preferences

    def _sync_scalar_field(self, scope: MemoryScope, field: str, value: str | None) -> None:
        records = self._client.get_all(
            scope,
            extra_filters={**self._EXPLICIT_FILTER, "viknow_field": field},
        )
        normalized = (value or "").strip()
        if not normalized:
            for record in records:
                memory_id = record.get("id")
                if memory_id:
                    self._client.delete(str(memory_id))
            return
        metadata = {
            "viknow_source": MemorySource.USER_EXPLICIT.value,
            "viknow_field": field,
        }
        if records:
            memory_id = records[0].get("id")
            if memory_id and str(records[0].get("memory") or "") != normalized:
                self._client.update(str(memory_id), normalized)
            for duplicate in records[1:]:
                duplicate_id = duplicate.get("id")
                if duplicate_id:
                    self._client.delete(str(duplicate_id))
            return
        self._client.add(
            scope,
            [{"role": "user", "content": normalized}],
            infer=False,
            metadata=metadata,
        )

    def _sync_custom_notes(self, scope: MemoryScope, notes: list[str]) -> None:
        records = self._client.get_all(
            scope,
            extra_filters={**self._EXPLICIT_FILTER, "viknow_field": CUSTOM_NOTE_FIELD},
        )
        for record in records:
            memory_id = record.get("id")
            if memory_id:
                self._client.delete(str(memory_id))
        for note in notes:
            normalized = note.strip()
            if not normalized:
                continue
            self._client.add(
                scope,
                [{"role": "user", "content": normalized}],
                infer=False,
                metadata={
                    "viknow_source": MemorySource.USER_EXPLICIT.value,
                    "viknow_field": CUSTOM_NOTE_FIELD,
                },
            )


def _preferences_from_records(records: list[dict[str, Any]]) -> UserPreferences:
    persona: str | None = None
    occupation: str | None = None
    answer_style: str | None = None
    custom_notes: list[str] = []
    for record in records:
        metadata = record.get("metadata") or {}
        field = metadata.get("viknow_field")
        text = str(record.get("memory") or "").strip()
        if not text:
            continue
        if field == "persona":
            persona = text
        elif field == "occupation":
            occupation = text
        elif field == "answer_style":
            answer_style = text
        elif field == CUSTOM_NOTE_FIELD:
            custom_notes.append(text)
    return UserPreferences(
        persona=persona,
        occupation=occupation,
        answer_style=answer_style,
        custom_notes=custom_notes,
    )


def _format_preference_context(
    explicit_records: list[dict[str, Any]],
    extracted_records: list[dict[str, Any]],
) -> str:
    explicit_lines = _format_explicit_lines(explicit_records)
    extracted_lines = _format_memory_lines(extracted_records)
    if not explicit_lines and not extracted_lines:
        return ""
    parts = ["## 用户偏好"]
    if explicit_lines:
        parts.append("### 用户自定义设置（请严格遵循原文）")
        parts.extend(explicit_lines)
    if extracted_lines:
        parts.append("### 从历史对话推断的偏好（如有冲突以用户自定义为准）")
        parts.extend(extracted_lines)
    return "\n".join(parts)


def _format_memory_context(
    fact_records: list[dict[str, Any]],
    episodic_records: list[dict[str, Any]],
) -> str:
    fact_lines = _format_memory_lines(fact_records)
    episodic_lines = _format_memory_lines(episodic_records)
    if not fact_lines and not episodic_lines:
        return ""
    parts: list[str] = []
    if fact_lines:
        parts.append("## 用户背景")
        parts.extend(fact_lines)
    if episodic_lines:
        parts.append("## 相关经历")
        parts.extend(episodic_lines)
    return "\n".join(parts)


def _format_explicit_lines(records: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for record in records:
        metadata = record.get("metadata") or {}
        field = str(metadata.get("viknow_field") or "")
        text = str(record.get("memory") or "").strip()
        if not text:
            continue
        if field == CUSTOM_NOTE_FIELD:
            lines.append(f"- {text}")
            continue
        label = PREFERENCE_FIELD_LABELS.get(field, field or "偏好")
        lines.append(f"- 【{label}】{text}")
    return lines


def _format_memory_lines(records: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for record in records:
        text = str(record.get("memory") or "").strip()
        if text:
            lines.append(f"- {text}")
    return lines
