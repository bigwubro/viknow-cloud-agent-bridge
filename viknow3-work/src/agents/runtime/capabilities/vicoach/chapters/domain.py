"""Chapter model settings read from app.yaml."""

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChapterConfig(Model):
    ref: str | None = Field(default=None, min_length=1)
    base_url: str | None = None
    base_url_env: str | None = None
    model: str | None = None
    model_env: str | None = None
    token_budget_mode: Literal["vllm", "conservative"] = "vllm"
    image_tokens: int = Field(default=8192, gt=0)
    tokenizer_url: str | None = None
    api_key_env: str | None = None
    context_tokens: int = Field(gt=0)
    input_tokens: int = Field(default=8192, ge=1024)
    output_tokens: int = Field(default=8192, ge=256)
    boundary_output_tokens: int = Field(default=2048, ge=256)
    safety_tokens: int = Field(default=512, ge=0)
    overlap_events: int = Field(default=3, ge=1)
    timeout_seconds: float = Field(default=300, gt=0)
    retries: int = Field(default=1, ge=0, le=3)
    source_manifest: Path | None = None
    visual_review: bool = False
    visual_review_max_events: int = Field(default=3, ge=1)

    def materialize(self):
        """Freeze endpoint/model before durable submission; retain only the key env name."""
        if self.ref:
            raise ValueError("chapter model ref must be resolved by AppConfig")
        values = {}
        for name in ("model", "base_url"):
            explicit = getattr(self, name)
            env_name = getattr(self, name + "_env")
            value = (
                explicit.strip()
                if explicit and explicit.strip()
                else (os.getenv(env_name, "").strip() if env_name else "")
            )
            if not value:
                raise ValueError(
                    f"chapter {name} or its configured environment variable is required"
                )
            values[name] = value
            values[name + "_env"] = None
        return type(self).model_validate({**self.model_dump(), **values})

    @model_validator(mode="after")
    def capacity(self):
        if self.ref and any(
            getattr(self, name) is not None
            for name in (
                "base_url",
                "base_url_env",
                "model",
                "model_env",
                "api_key_env",
                "tokenizer_url",
            )
        ):
            raise ValueError(
                "chapter ref cannot be combined with explicit model connection settings"
            )
        if not self.ref and (
            not (self.model or self.model_env) or not (self.base_url or self.base_url_env)
        ):
            raise ValueError("chapter model and base_url (or their environment names) are required")
        if (
            self.context_tokens
            <= max(self.output_tokens, self.boundary_output_tokens) + self.safety_tokens + 1024
        ):
            raise ValueError("context window cannot fit configured output reserve and input")
        return self
