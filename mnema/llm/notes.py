"""How the title and summary went when the active model failed.

Data, not text: live/remarks words it for the user (what it means for the result and what to do).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from .errors import ErrorKind


class Cloud(StrEnum):
    FAILED = "failed"  # the cloud refused (cloud_detail says why)
    NOT_SET = "not_set"  # no cloud model set, the cloud was not called


class LightNote(BaseModel):
    engine: str  # cloud | none
    cloud: Cloud
    cloud_detail: str = ""  # redacted
    kind: ErrorKind | None = None  # how the cloud model refused
    until: datetime | None = None  # when the limit resets
    model: str = ""  # the active model that worked or tried
