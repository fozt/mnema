"""Pydantic models of REST request bodies."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..i18n import _

ONE_INPUT = "A path or non-empty tracks are needed"


class JobRequest(BaseModel):
    """A job takes one path or uploaded tracks, and the operation parameters."""

    model_config = ConfigDict(extra="forbid")

    op: str
    path: str = ""
    tracks: dict[Literal["mic", "system"], str] | None = None
    key: str | None = Field(default=None, max_length=128)
    params: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def one_input(self) -> Self:
        if self.tracks is not None:
            if self.path or not self.tracks or not all(self.tracks.values()):
                raise ValueError(_(ONE_INPUT))
        elif not self.path:
            raise ValueError(_(ONE_INPUT))
        return self
