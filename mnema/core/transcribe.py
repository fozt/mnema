"""Transcription: pick a backend and recognize, in a separate process."""

from __future__ import annotations

from pathlib import Path

from ..i18n import _
from .backends import AUTO, engine, select_backend
from .base import MediaError, Progress, noop
from .models import Transcript
from .probe import probe
from .settings import requested_asr_threads


def transcribe(
    src: Path,
    *,
    backend: str = AUTO,
    model: str | None = None,
    lang: str | None = None,
    on: Progress = noop,
) -> Transcript:
    """Recognize in a separate process, so all ASR memory is freed before the result returns."""
    from .worker import run

    if backend != AUTO and not select_backend(backend, model=model).available():
        found = engine(backend)
        title = found.title if found is not None else backend
        raise MediaError(_("{name} recognition is not installed on this computer", name=title))

    result = run(
        "transcribe",
        {
            "src": str(src.resolve()),
            "backend": str(backend),
            "model": model,
            "lang": lang,
            "threads": requested_asr_threads(),
        },
        on=on,
    )
    return Transcript.model_validate(result)


def _transcribe(
    src: Path,
    *,
    backend: str = AUTO,
    model: str | None = None,
    lang: str | None = None,
    on: Progress = noop,
) -> Transcript:
    """Worker body: pick the engine and recognize.

    A given language is used as is, unchecked. Without one, nothing is detected up front:
    Parakeet recognizes without a hint and names the language from the text, Whisper
    detects it itself.
    """
    info = probe(src)
    if not info.has_audio:
        raise MediaError(_("The file has no audio track: {name}", name=src.name))
    be = select_backend(backend, model=model, lang=lang)
    tr = be.run(src, lang=lang, total_duration=info.duration, on=on)
    tr.engine = be.name
    return tr
