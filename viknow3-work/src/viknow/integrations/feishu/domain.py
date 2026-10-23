"""Typed Feishu Wiki response models used by the connector."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _FeishuModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class FeishuSpace(_FeishuModel):
    space_id: str
    name: str = ""
    description: str = ""
    space_type: str | None = None


class FeishuWikiNode(_FeishuModel):
    node_token: str
    space_id: str
    obj_token: str
    obj_type: str
    title: str = ""
    parent_node_token: str | None = None
    has_child: bool = False
    node_type: str | None = None
    origin_node_token: str | None = None
    origin_space_id: str | None = None


class FeishuWikiNodePage(_FeishuModel):
    items: list[FeishuWikiNode] = Field(default_factory=list)
    has_more: bool = False
    page_token: str | None = None
