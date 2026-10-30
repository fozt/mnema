"""The operation registry: a declarative @op catalog (name → function, parameter schema,
input and output kind).

A leaf module without UI or FastAPI: the server and the CLI import the registry, the thin
client only metadata and functions, without engines. Heavy and platform imports stay lazy.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from . import core as _mc
from .core.base import LANG_NAMES, MediaError, Progress, phased, tool
from .i18n import _
from .library.models import FileName
from .llm import claude as _claude
from .llm.errors import human_reason

logger = logging.getLogger(__name__)


class InputKind(StrEnum):
    MEDIA = "media"  # a resolved path to a local or server file
    TEXT = "text"  # a transcript or text sidecar (may need transcribing first)
    TRACKS = "tracks"  # the uploaded tracks of one recording


class OutputKind(StrEnum):
    JSON = "json"  # a structured dict, returned inline
    TEXT = "text"  # markdown or text, returned inline (wrapped in a dict)
    FILE = "file"  # makes file(s) on disk; the result carries the path(s) for /api/download


OpFn = Callable[[Path, dict, "Progress"], dict]


class OpSpec(BaseModel):
    """One entry of the operation registry."""

    model_config = {"arbitrary_types_allowed": True}

    name: str
    fn: OpFn
    params: type[BaseModel]
    input_kind: InputKind
    output_kind: OutputKind
    # The operation needs only the sound (transcription and what builds on it): a remote
    # client may extract the audio from a video and upload just that. False when the picture
    # is needed (compress).
    audio_only: bool = False
    requires_model: bool = False


_REGISTRY: dict[str, OpSpec] = {}


def op(
    name: str,
    *,
    params: type[BaseModel],
    input: InputKind | str,
    output: OutputKind | str,
    audio_only: bool = False,
    requires_model: bool = False,
) -> Callable[[OpFn], OpFn]:
    """Registers the function as an op (a decorator); a repeated name is an error."""

    def decorator(fn: OpFn) -> OpFn:
        if name in _REGISTRY:
            raise ValueError(f"operation already registered: {name}")
        _REGISTRY[name] = OpSpec(
            name=name,
            fn=fn,
            params=params,
            input_kind=InputKind(input),
            output_kind=OutputKind(output),
            audio_only=audio_only,
            requires_model=requires_model,
        )
        return fn

    return decorator


def registry() -> dict[str, OpSpec]:
    """The operation registry (name → OpSpec): metadata and functions, no side effects."""
    return dict(_REGISTRY)


def get(name: str) -> OpSpec | None:
    return _REGISTRY.get(name)


SERVER_NO_MODEL = "No model is configured on the server"
MAC_NO_MODEL = "No model configured. Click “Choose model…” in Mnema settings"


def no_model() -> str:
    """The refusal without a cloud model, by where processing runs: on this Mac, where to choose
    one; on the server, that there is none."""
    from .core import base

    return _(MAC_NO_MODEL) if base.processed_here() else _(SERVER_NO_MODEL)


class Refused(MediaError):
    """The job is refused before work: the text for the user and the API status (400, 422)."""

    def __init__(self, text: str, status: int) -> None:
        super().__init__(text)
        self.status = status


def admit(spec: OpSpec, values: dict) -> dict:
    """Admission before work, one rule for the server API and processing on the Mac:
    parameters by the operation's schema (422); a recording needs available recognition (422);
    a recording summary needs a model (400). The API refuses cloud operations (requires_model)
    earlier, before checking the input. Returns the parameters as the operation gets them."""
    from . import node_settings
    from .llm.config import LLMSettings

    name = spec.name
    try:
        params = spec.params.model_validate(values).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        errors = exc.errors()
        # a validator's own text as is; a wrong type or an extra field by the parameter name
        own = [e["msg"].removeprefix("Value error, ") for e in errors if e["type"] == "value_error"]
        fields = ", ".join(dict.fromkeys(".".join(map(str, e["loc"])) for e in errors))
        detail = ". ".join(dict.fromkeys(own)) or _(
            "Invalid processing parameter: {fields}", fields=fields
        )
        raise Refused(detail, 422) from exc
    if name == "recording" and (
        problem := node_settings.asr_problem(params["asr"], params.get("lang"))
    ):
        raise Refused(problem, 422)
    if name == "recording_summary" and not params["finalize"] and not LLMSettings().cloud:
        raise Refused(no_model(), 400)
    return params


# extra="forbid": a typo in a parameter is refused, not silently dropped.
_STRICT = {"extra": "forbid"}


class TranscribeParams(BaseModel):
    model_config = _STRICT

    backend: str = "auto"
    model: str | None = None
    lang: str | None = None


class TranslateParams(BaseModel):
    model_config = _STRICT

    to_lang: str = "ru"


class CompressParams(BaseModel):
    model_config = _STRICT

    fps: int = 30
    quality: int = 60


class Mp3Params(BaseModel):
    model_config = _STRICT

    bitrate: str = "128k"


class Part(BaseModel):
    model_config = _STRICT

    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _ordered(self) -> Part:
        if self.end <= self.start:
            raise ValueError(_("The end of the part must be after its start"))
        return self


def _has_control(value: str) -> bool:
    return any(unicodedata.category(c) in {"Cc", "Zl", "Zp"} for c in value)


def _folder_name(value: str) -> bool:
    """A recording folder name: not empty or hidden, no path or control characters, ≤ 255 bytes."""
    from .live.naming import NAME_MAX_BYTES

    if not value.strip() or value.startswith(".") or _has_control(value):
        return False
    try:
        return not any(c in "/\\" for c in value) and len(value.encode()) <= NAME_MAX_BYTES
    except UnicodeEncodeError:  # a lone surrogate
        return False


def _title_line(value: str | None) -> str | None:
    # A human title: "/" is fine, folder_title turns it into a folder name.
    if value is not None and _has_control(value):
        raise ValueError(_("The title has characters that are not allowed"))
    return value


class _RecordingBase(BaseModel):
    model_config = _STRICT

    started: AwareDatetime
    name: str  # the recording folder name
    summary_lang: str | None = None

    @field_validator("name")
    @classmethod
    def _folder(cls, value: str) -> str:
        if not _folder_name(value):
            raise ValueError(_("The folder name has characters that are not allowed"))
        return value

    @field_validator("summary_lang")
    @classmethod
    def _language(cls, value: str | None) -> str | None:
        if value is not None and value not in LANG_NAMES:
            raise ValueError(_("Unknown language"))
        return value


class _RecordingFinalization(_RecordingBase):
    translate: str | None = None
    user_title: str | None = Field(default=None, max_length=500)
    retitle: bool | None = None
    lang: str | None = None
    summary: bool = True
    recording_error: str | None = Field(default=None, max_length=500)

    @field_validator("user_title")
    @classmethod
    def _user_title(cls, value: str | None) -> str | None:
        return _title_line(value)

    @model_validator(mode="after")
    def _own_title(self) -> _RecordingFinalization:
        from .live.naming import chosen_title, part_label

        # nothing is left for a folder name ("???", only a part mark): no own title
        self.user_title = chosen_title(self.user_title, part_label(self.name))
        return self

    @field_validator("lang", "translate")
    @classmethod
    def _call_language(cls, value: str | None) -> str | None:
        return cls._language(value)


class RecordingParams(_RecordingFinalization):
    asr: str = "auto"
    part: Part | None = None
    # why the other side had no sound at all (the phone's report): blocked — a call was going on,
    # none — there was no call; refines the remark on the silence the server found
    silent_reason: dict[Literal["system"], Literal["blocked", "none"]] = Field(default_factory=dict)
    tracks: dict[str, str] = Field(default_factory=dict)
    partial: bool = False  # a track failed: True keeps the others' text, False fails without files


class RecordingSummaryParams(_RecordingFinalization):
    finalize: bool = False  # finishing live text without WAV; default is an explicit re-summary
    past: dict | None = None


class ResultFile(StrEnum):
    """Recording files a recording job may return."""

    TRANSCRIPT = FileName.TRANSCRIPT.value
    SUMMARY = FileName.SUMMARY.value
    TRANSLATION = "translation.md"


class RecordingResult(BaseModel):
    """A recording job's result (result in GET /api/jobs/{id}): the recording files as text or
    None (the file is gone), the new folder name (None keeps it) and the history run
    (meta.Run)."""

    files: dict[ResultFile, str | None] = Field(default_factory=dict)
    folder: str | None = None
    run: dict

    @field_validator("folder", mode="after")
    @classmethod
    def _folder(cls, value: str | None) -> str | None:
        # an invalid name does not rename the folder: the texts matter more than the name
        return value if value is not None and _folder_name(value) else None


class RecordingSummaryResult(RecordingResult):
    @field_validator("files", mode="after")
    @classmethod
    def _keeps_transcript(cls, files: dict) -> dict:
        if ResultFile.TRANSCRIPT in files:
            raise ValueError(_("A summary must not change the transcript"))
        return files


def recording_result(op: str) -> type[RecordingResult]:
    """The result schema of recording op that the client reads it by."""
    return RecordingSummaryResult if op == "recording_summary" else RecordingResult


TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown"})


def _transcript_text(path: Path, on: Progress) -> str:
    # Ready text (a transcript, translation or notes) is read as is, not through ffprobe.
    if path.suffix.lower() in TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8")
    txt = path.with_suffix(".txt")
    if txt.exists():
        return txt.read_text(encoding="utf-8")
    on(None, _("Recognizing speech first…"))
    return _mc.transcribe(path, on=on).text


@op(
    "recording",
    params=RecordingParams,
    input=InputKind.TRACKS,
    output=OutputKind.JSON,
    audio_only=True,
)
def _recording(p: Path, params: dict, on: Progress) -> dict:
    from .live.node_process import process_recording

    return process_recording(p, params, on)


@op(
    "recording_summary",
    params=RecordingSummaryParams,
    input=InputKind.TEXT,
    output=OutputKind.JSON,
    audio_only=True,
)
def _recording_summary(p: Path, params: dict, on: Progress) -> dict:
    from .live.node_process import summarize_recording

    return summarize_recording(p, params, on)


@op(
    "transcribe",
    params=TranscribeParams,
    input=InputKind.MEDIA,
    output=OutputKind.JSON,
    audio_only=True,
)
def _transcribe(p: Path, params: dict, on: Progress) -> dict:
    # mode="json": Path and other non-JSON types become JSON-native (a job result is JSON)
    return _mc.transcribe(p, on=on, **params).model_dump(mode="json")


@op(
    "translate",
    params=TranslateParams,
    input=InputKind.TEXT,
    output=OutputKind.TEXT,
    audio_only=True,
    requires_model=True,
)
def _translate(p: Path, params: dict, on: Progress) -> dict:
    p_tr, p_llm = phased(on, 0.85, 0.15)  # transcription 0–85%, translation 85–100%
    text = _transcript_text(p, p_tr)
    try:
        return {"markdown": _claude.translate(text, on=p_llm, **params)}
    except MediaError:
        raise
    except Exception as exc:  # noqa: BLE001 - the model's refusal in the user's words
        raise MediaError(_("Translation failed: {reason}", reason=human_reason(exc))) from exc


@op("compress", params=CompressParams, input=InputKind.MEDIA, output=OutputKind.FILE)
def _compress(p: Path, params: dict, on: Progress) -> dict:
    return _mc.compress(p, p.with_name(p.stem + "_compressed.mov"), on=on, **params).model_dump(
        mode="json"
    )


@op("mp3", params=Mp3Params, input=InputKind.MEDIA, output=OutputKind.FILE)
def _mp3(p: Path, params: dict, on: Progress) -> dict:
    from .core.video import _ffmpeg_with_progress  # parses -progress: a bar by file time

    out = p.with_suffix(".mp3")
    bitrate = params.get("bitrate", "128k")
    on(None, _("Converting to mp3…"))
    total = _mc.probe(p).duration or 0.0
    _ffmpeg_with_progress(
        [tool("ffmpeg"), "-y", "-i", str(p), "-vn", "-b:a", bitrate, str(out)], total=total, on=on
    )
    return {"path": str(out)}
