"""The Mac's Recognizer: mnema-asr (Parakeet Ultra on CoreML through FluidAudio) of Mnema.app.

The helper is its own process, started from this one (no Python child): one JSON request per
line on stdin, replies on stdout, the envelope of core/worker.py; the contract is
tests/fixtures/asr-helper-v1.json. The helper sends stages and error codes, the texts are here.
Only "download" touches the network: a missing model is downloaded when this Mac is to
recognize (the setting is turned on, or the first recording without the model).
"""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..i18n import _
from . import base, parakeet
from .asr_engine import SR, Word
from .base import MediaError, Progress, noop

logger = logging.getLogger(__name__)

ENV = "MNEMA_ASR"  # the helper's path; Mnema.app sets it for its engine
PROTOCOL = 1
RECOGNIZER = "parakeet"
VAD = "vad"  # speech search for the cloud without a server, a part of every recognizer
_EBADARCH = 86  # exec of an arm64 program on an Intel Mac

NO_HELPER = "Install Mnema or connect a server"
UPDATE_APP = "Update Mnema to recognize on this Mac"
UPDATE_COMMAND = "Update the mnema command to recognize on this Mac"
FAILED = "Could not recognize on this Mac. See the Mnema log for details"
_ERRORS = {
    "no_model": "The recognition model is not downloaded",
    "no_space": "Not enough disk space for the recognition model",
    "download_failed": "Could not download the recognition model. Check the internet connection",
    "bad_audio": "Could not read the recording audio",
    "unsupported_mac": "Recognition on this Mac needs Apple silicon",
}
_STAGES = {
    "download": "Downloading the recognition model, only the first time…",
    "load": "Loading the recognition model…",
    "recognize": "Recognizing…",
}


class HelperError(MediaError):
    """A refusal of mnema-asr by its code; the text is the user's, the detail is in the log."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_(_ERRORS.get(code, FAILED)))

    @property
    def known(self) -> bool:
        """The cause is in the user's words, not just "failed"."""
        return self.code in _ERRORS


class Model(BaseModel):
    """A recognition model the helper knows (the models operation)."""

    id: str
    title: str
    langs: list[str]
    size: int  # bytes on disk once downloaded
    installed: bool


def helper_path() -> Path | None:
    """mnema-asr: MNEMA_ASR as is (also when it is missing), else the one app.json names, else
    the one of Mnema.app in /Applications or ~/Applications (base.mac_app)."""
    given = os.environ.get(ENV, "").strip()
    if given:
        return Path(given) if _runnable(Path(given)) else None
    named = base.installed_app().get("asr")
    if named and _runnable(Path(named)):
        return Path(named)
    bundle = base.mac_app()
    found = bundle / "Contents" / "MacOS" / "mnema-asr" if bundle is not None else None
    return found if found is not None and _runnable(found) else None


def _runnable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _apple_silicon() -> bool:
    return platform.machine() == "arm64"


def _call(operation: str, payload: dict[str, Any], on: Progress) -> Any:
    """One request to a fresh helper; progress goes to on, an exception from on stops it."""
    from .worker import Worker

    helper = helper_path()
    if helper is None:
        raise MediaError(_(NO_HELPER))
    if not _apple_silicon():
        raise HelperError("unsupported_mac")
    status: tuple[float | None, str] = (None, _("Preparing recognition…"))
    on(*status)
    try:
        worker = Worker(argv=[str(helper)])
    except OSError as e:
        logger.warning("mnema-asr did not start", exc_info=True)
        raise HelperError("unsupported_mac" if e.errno == _EBADARCH else "failed") from e
    with worker:
        worker.send({"operation": operation, "payload": payload})
        while True:
            event = worker.receive()
            if event is None:
                on(*status)  # a stop also works during a long download or window
                continue
            if "v" not in event and "message" in event:  # the Worker's own: a corrupt reply
                raise MediaError(event["message"])
            if event.get("v") != PROTOCOL:
                newer = isinstance(event.get("v"), int) and event["v"] > PROTOCOL
                raise MediaError(_(UPDATE_COMMAND) if newer else _(UPDATE_APP))
            match event.get("type"):
                case "progress":
                    stage = _STAGES.get(event.get("stage", ""), _STAGES["recognize"])
                    status = event.get("fraction"), _(stage)
                    on(*status)
                case "result":
                    return event.get("value")
                case "error":
                    raise HelperError(str(event.get("code")))
                case _:
                    raise MediaError(_("Unknown reply from the recognition process"))


def models() -> list[Model]:
    """The helper's models and whether they are downloaded; no network."""
    return [Model.model_validate(m) for m in _call("models", {}, noop)]


def download(model: str, *, on: Progress = noop) -> None:
    """Download a model with its parts (the speech model comes with a recognizer); a model
    already there costs a check only. A second process waits for the first one's download."""
    _call("download", {"id": model}, on)


def _downloaded(model: str, operation: str, payload: dict[str, Any], on: Progress) -> Any:
    """The operation; a missing model is downloaded first, with the same progress."""
    try:
        return _call(operation, payload, on)
    except HelperError as e:
        if e.code != "no_model":
            raise
    logger.info("mnema-asr: %s is not downloaded, downloading it", model)
    download(model, on=on)
    return _call(operation, payload, on)


class MacHelper:
    """The Recognizer of the Mac: mnema-asr of the installed app."""

    id: str = RECOGNIZER
    model: str = parakeet.MODEL
    langs: frozenset[str] = parakeet.LANGS

    def available(self) -> bool:
        """The helper is there and can run here; the model may still have to be downloaded."""
        return helper_path() is not None and _apple_silicon()

    def words(self, wav: Path, *, on: Progress = noop) -> list[Word]:
        return self.tracks([wav], on=on)[0]

    def tracks(self, wavs: list[Path], *, on: Progress = noop) -> list[list[Word]]:
        """The words of each track: one request, so the model loads once for all of them."""
        payload = {"id": self.id, "wavs": [str(w) for w in wavs]}
        value = _downloaded(self.id, "transcribe", payload, on)
        return [[Word.model_validate(w) for w in t["words"]] for t in value["tracks"]]

    def speech(self, wav: Path, *, max_s: float) -> list[tuple[int, int]]:
        """FluidAudio's speech segments, a longer one cut into equal parts of at most max_s."""
        value = _downloaded(VAD, "speech", {"wav": str(wav)}, noop)
        limit = max(1, int(max_s * SR))
        spans: list[tuple[int, int]] = []
        for start, end in value["segments"]:
            parts = -(-(end - start) // limit)
            spans += [
                (start + (end - start) * i // parts, start + (end - start) * (i + 1) // parts)
                for i in range(parts)
            ]
        return spans
