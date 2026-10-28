"""Processing history of a recording: .mnema-meta.json in its folder.

Every processing (after recording, "Process again", `mnema finish`) appends a run: when and with
which version it ran, its settings, the result per track (engine, language) and for the summary,
step timings, remarks and the outcome (ok/stopped/failed). No keys or tokens: the processing
server is only a host. The file is written whole through a temporary file next to it
(tempfile + fsync + replace); a write failure never fails the processing.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from .remarks import Remark

logger = logging.getLogger(__name__)

META_FILE = ".mnema-meta.json"
VERSION = 1


class TrackRun(BaseModel):
    engine: str | None = None  # gigaam | mlx | whisperx; None — unknown
    lang: str | None = None
    seconds: float | None = None
    error: str | None = None  # why the track was not recognized


class LightRun(BaseModel):
    """How the summary was made."""

    engine: str  # cloud | none
    model: str | None = None  # the active model; None — none chosen
    lang: str | None = None


class Run(BaseModel):
    kind: Literal["process", "summary"] = "process"  # summary — "Summarize"
    job: str | None = None
    started: str = ""
    finished: str = ""
    status: Literal["ok", "stopped", "failed"] | None = None
    mnema: dict[str, str] = Field(default_factory=dict)  # version, git
    settings: dict[str, Any] = Field(default_factory=dict)
    part: dict[str, float] | None = None  # part of the recording: start, end (s)
    tracks: dict[str, TrackRun] = Field(default_factory=dict)
    summary: LightRun | None = None
    steps: dict[str, float] = Field(default_factory=dict)  # step → seconds
    remarks: list[str] = Field(default_factory=list)
    notes: list[Remark] = Field(default_factory=list)  # remarks with their steps, before merging
    info: list[str] = Field(default_factory=list)
    error: str | None = None


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@functools.cache
def mnema_version() -> dict[str, str]:
    """Package version and, for a repository checkout, the short commit sha."""
    import importlib.metadata

    out: dict[str, str] = {}
    try:
        out["version"] = importlib.metadata.version("mnema")
    except importlib.metadata.PackageNotFoundError:
        pass
    root = Path(__file__).resolve().parents[2]
    if (root / ".git").exists():
        try:
            sha = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
            ).stdout.strip()  # fmt: skip
        except (OSError, subprocess.SubprocessError):
            sha = ""
        if sha:
            out["git"] = sha
    return out


def host(url: str | None) -> str | None:
    """The processing server without scheme, path or credentials: "203.0.113.10:8000"."""
    if not url:
        return None
    parts = urlsplit(url if "//" in url else f"//{url}")
    if not parts.hostname:
        return None
    return f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname


def begin(config: Any, *, part: dict[str, float] | None = None) -> Run:
    """Start of a processing: time, version and settings (LiveConfig + the active model)."""
    run = Run(started=now(), part=part)
    try:
        from ..llm.config import LLMSettings

        llm = LLMSettings()
        run.mnema = mnema_version()
        run.settings = {
            "lang": config.lang,
            "asr": config.asr,
            "summary": config.summary,
            "summary_lang": config.summary_lang,
            "server": host(config.runner),
            "model": llm.model,  # the active cloud model; None — none chosen
            "source": llm.source,  # env | unconfigured
        }
    except Exception:  # noqa: BLE001 - history is informational, it never fails processing
        logger.warning("could not collect settings for the processing history", exc_info=True)
    return run


class RecordState(StrEnum):
    """Recording state (fixture record-state-v1, ported to the phone as RecordState.kt)."""

    UNPROCESSED = "unprocessed"  # sound, no processing outcome
    PROCESSING = "processing"
    PROCESSED = "processed"  # the last processing finished, there is text
    NO_SPEECH = "no_speech"  # finished, no speech
    AUDIO_ONLY = "audio_only"  # processing was stopped with no outcome before it
    FAILED = "failed"  # the last processing failed


class Work(StrEnum):
    """This device's work file for the recording: its work is running, broke off, or none."""

    NONE = "none"
    RUNNING = "running"
    INTERRUPTED = "interrupted"


UNFINISHED = frozenset({RecordState.UNPROCESSED, RecordState.FAILED})  # waiting for "Process…"
_TEXTS = ("transcript.txt", "summary.txt")


def _process_runs(data: Any) -> list[dict]:
    runs = data.get("runs") if isinstance(data, dict) else None
    return [
        r
        for r in (runs if isinstance(runs, list) else [])
        if isinstance(r, dict) and r.get("kind", "process") in (None, "process")
    ]


def _outcomes(data: Any) -> list[dict]:
    return [r for r in _process_runs(data) if r.get("status") in ("ok", "failed", "stopped")]


def outcome(data: Any) -> dict | None:
    """The processing outcome from the history (data — parsed meta): the last processing (not
    "Summarize") that ended ok or failed; a stopped one decides only when no outcome came before
    it. None — no processing in the history."""
    runs = _outcomes(data)
    finished = [r for r in runs if r["status"] != "stopped"]
    return (finished or runs or [None])[-1]


def processed(data: Any) -> bool:
    """The last processing, a stopped one too, finished: "Delete audio of processed recordings"
    (fixture audio-cleanup-v1) keeps the audio while a new processing was stopped."""
    runs = _outcomes(data)
    return bool(runs) and runs[-1]["status"] == "ok"


def has_sound(sizes: Mapping[str, int]) -> bool:
    """Sound in the folder: a WAV longer than its header or a non-empty file of another audio
    format."""
    from ..core.base import AUDIO_EXT
    from .naming import WAV_HEADER

    return any(
        size > (WAV_HEADER if name.lower().endswith(".wav") else 0)
        for name, size in sizes.items()
        if Path(name).suffix.lower() in AUDIO_EXT
    )


def record_state(sizes: Mapping[str, int], data: Any, work: Work = Work.NONE) -> RecordState:
    """The one rule for a recording's state: sizes — folder files (name → size), data — parsed
    meta, work — this device's running work. The outcome comes from the history (outcome); work
    files hold no outcome: broken-off work means unprocessed. No outcome in the history (a
    `mnema live` draft, a folder assembled by hand): text means processed; without sound there
    is nothing to process."""
    if work is Work.RUNNING:
        return RecordState.PROCESSING
    sound = has_sound(sizes)
    if work is Work.INTERRUPTED and sound:
        return RecordState.UNPROCESSED
    text = any(name in sizes for name in _TEXTS)
    run = outcome(data)
    if run is not None:
        if run["status"] == "failed":
            return RecordState.FAILED
        if text:  # stopped with no outcome before it: text means processed
            return RecordState.PROCESSED
        return RecordState.AUDIO_ONLY if run["status"] == "stopped" else RecordState.NO_SPEECH
    if text:
        return RecordState.PROCESSED
    return RecordState.UNPROCESSED if sound else RecordState.NO_SPEECH  # nothing to process


def _shown(data: Any) -> dict | None:
    """The last processing in the history except a stopped one (nothing to say about it): the
    remarks are about it, and "Summarize" takes it as the previous one (fixture shown-run-v1)."""
    runs = data.get("runs") if isinstance(data, dict) else None
    shown = [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []
    return next((r for r in reversed(shown) if r.get("status") != "stopped"), None)


def info_of(data: Any) -> list[str]:
    """Info lines of the same processing as remarks_of."""
    info = (_shown(data) or {}).get("info")
    return [n for n in info if isinstance(n, str)] if isinstance(info, list) else []


def remarks_of(data: Any) -> list[str]:
    """Remarks of the processing from the history (_shown) as full text: notes are the source
    of truth, the wording comes from remarks.py; a failure reason with no remark about it is
    added as is, as on the phone."""
    from .remarks import merged

    last = _shown(data)
    if last is None:
        return []
    notes: list[Remark] = []
    for note in last.get("notes") or []:
        try:
            notes.append(Remark.model_validate(note))
        except ValueError:
            continue
    result = [r.text for r in merged(notes)]
    error = last.get("error")
    if isinstance(error, str) and error.strip() and all(error not in t for t in result):
        result.append(error)
    return result


def amend(base: Path, *, notes: Sequence[Remark] = (), info: Sequence[str] = ()) -> None:
    """Add to the last processing in the history what the client learned after the node's
    result: remarks (the combined mp3 failed) and info (part of a recording, full one in the
    Trash)."""
    if not notes and not info:
        return
    data = _for_write(base)
    if data is None or not data["runs"] or not isinstance(data["runs"][-1], dict):
        return
    last = data["runs"][-1]
    last["notes"] = [*last.get("notes", []), *(r.model_dump(mode="json") for r in notes)]
    last["remarks"] = [*last.get("remarks", []), *(r.text for r in notes)]
    last["info"] = list(dict.fromkeys([*last.get("info", []), *info]))
    _write(base, data)


def last(base: Path) -> Run | None:
    """The last processing in the folder's history except a stopped one (_shown); None when
    there is no readable history."""
    try:
        shown = _shown(json.loads((base / META_FILE).read_text(encoding="utf-8")))
        return Run.model_validate(shown) if shown is not None else None
    except (OSError, ValueError, LookupError, TypeError):
        return None


def started(base: Path) -> str | None:
    """The recording start in its original time zone, apart from when it was processed."""
    try:
        value = json.loads((base / META_FILE).read_text(encoding="utf-8")).get("started")
        return value if isinstance(value, str) and value.strip() else None
    except (OSError, ValueError, AttributeError):
        return None


def set_started(base: Path, iso: str) -> None:
    data = _for_write(base)
    if data is not None:
        data["started"] = iso
        _write(base, data)


def append(base: Path, run: Run) -> None:
    """Append a processing, keeping unknown fields."""
    data = _for_write(base)
    if data is not None:
        data["runs"].append(run.model_dump(mode="json"))
        _write(base, data)


def _for_write(base: Path) -> dict[str, Any] | None:
    """An unreadable history is kept next to it; data of a newer version is left alone."""
    path = base / META_FILE
    data: Any = {"version": VERSION, "runs": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and data.get("version") not in (None, VERSION):
            logger.warning("%s: history version %s, not appending", path, data["version"])
            return None
        if not isinstance(data, dict) or not isinstance(data.get("runs", []), list):
            try:
                path.replace(path.with_name(f"{META_FILE}.bad"))
            except OSError:
                logger.warning("unreadable processing history %s", path, exc_info=True)
                return None
            data = {}
    data.setdefault("version", VERSION)
    data.setdefault("runs", [])
    return data


def _write(base: Path, data: dict[str, Any]) -> None:
    path = base / META_FILE
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp: str | None = None
    try:
        fd, tmp = tempfile.mkstemp(dir=base, prefix=f"{META_FILE}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        logger.warning("could not write the processing history %s", path, exc_info=True)
        if tmp is not None:
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                logger.debug("could not remove the temporary history %s", tmp, exc_info=True)
