"""Optional application capabilities, each owning its own configuration."""

from pydantic import BaseModel, ConfigDict, Field

from .vicoach.config import ViCoachConfig


class CapabilitiesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vicoach: ViCoachConfig = Field(default_factory=ViCoachConfig)
