"""Live mode data models (pydantic v2)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

from pydantic import BaseModel, Field

from ..core.models import Segment
from ..i18n import _
from .remarks import Remark


class LiveChunk(BaseModel):
    """An incremental ASR result: committed segments plus the unstable draft tail."""

    committed: list[Segment] = Field(default_factory=list)
    draft: str = ""


class _StepMessages(Mapping[str, str]):
    """Step → its start message, read in the current language (the server picks it per job)."""

    _TEXTS = {
        "asr": "Recognizing speech…",
        "translate": "Translating…",
        "summary": "Writing the summary…",
        "save": "Saving…",
    }

    def __getitem__(self, step: str) -> str:
        return _(self._TEXTS[step])

    def __iter__(self) -> Iterator[str]:
        return iter(self._TEXTS)

    def __len__(self) -> int:
        return len(self._TEXTS)


# Processing steps after recording, in order. The progress scale is split evenly between them,
# and a step announces its start with STEP_MESSAGES[step]: a share alone cannot tell the steps
# apart (the end of one is the start of the next), while the menu and the panel show the
# current step and its own share.
FINALIZE_STEPS = ("asr", "translate", "summary", "save")
STEP_MESSAGES: Mapping[str, str] = _StepMessages()
TITLE_MESSAGE = "Coming up with a title…"  # the summary step when the summary is off


def title_message() -> str:
    """TITLE_MESSAGE in the current language."""
    return _(TITLE_MESSAGE)


class Track(BaseModel):
    """One source's track: its label (the speaker) and the committed segments so far."""

    label: str
    committed: list[Segment] = Field(default_factory=list)


class LiveResult(BaseModel):
    """The session result: tracks and paths of the saved artifacts.

    recordings — sound recordings: a WAV track per source (<base>.<label>.wav).
    transcript_txt — None when recognition produced no text (empty files are not created).
    refined_labels — tracks rebuilt by the offline pass over the recording.
    """

    tracks: list[Track] = Field(default_factory=list)
    recordings: list[Path] = Field(default_factory=list)
    transcript_txt: Path | None = None
    translation_md: Path | None = None
    summary_md: Path | None = None
    refined_labels: list[str] = Field(default_factory=list)
    silent_labels: list[str] = Field(default_factory=list)  # silent tracks, as decided by ASR
    remarks: list[Remark] = Field(default_factory=list)
    info: list[str] = Field(default_factory=list)  # notes: part cut out, WAV repaired
    # the server is unavailable and processing is deferred: the command to finish the folder
    defer_command: str | None = None
    base: Path | None = None  # the recording folder after processing (it may be renamed)
    languages: dict[str, str] = Field(default_factory=dict)  # track → transcript language

    @property
    def refine_warnings(self) -> list[str]:
        """Remarks as full text (terminal, "Details…")."""
        return [r.text for r in self.remarks]
