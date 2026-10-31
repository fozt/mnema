"""Wire contract of the file library; meta travels whole as a UTF-8 file."""

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field, model_validator

from ..i18n import _


class FileName(StrEnum):
    TRANSCRIPT = "transcript.txt"
    SUMMARY = "summary.txt"
    META = ".mnema-meta.json"


LEGACY_SUMMARY = "summary.md"  # earlier versions; becomes summary.txt when the recording is read


def recording_id(value: str) -> str:
    UUID(value)
    return value


RecordingId = Annotated[str, AfterValidator(recording_id)]
MtimeNs = Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]


class Stamp(BaseModel):
    sha256: str
    mtime_ns: MtimeNs


class Record(BaseModel):
    id: RecordingId
    path: str
    files: dict[FileName, Stamp]
    tag: str


class Manifest(BaseModel):
    records: list[Record] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)
    library: str = ""  # the user's library number on the server; empty from a 0.3 server


class FileBody(BaseModel):
    text: str
    mtime_ns: MtimeNs


class CreateRecord(BaseModel):
    model_config = {"extra": "forbid"}
    id: RecordingId
    path: str
    files: dict[FileName, FileBody]

    @model_validator(mode="after")
    def has_text_and_identity(self) -> "CreateRecord":
        if FileName.META not in self.files or not (
            {FileName.TRANSCRIPT, FileName.SUMMARY} & self.files.keys()
        ):
            raise ValueError(_("A recording needs meta and a text"))
        return self


class MoveRecord(BaseModel):
    model_config = {"extra": "forbid"}
    expected_path: str
    path: str
