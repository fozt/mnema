"""Recording operations of a node: temporary files and the shared finalize pipeline."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from .. import node_settings
from ..core import audio
from ..core.base import MediaError, Progress
from ..core.models import Segment
from ..i18n import _, catalog
from ..jobs import CANCEL_MESSAGE
from ..llm.config import LLMSettings
from ..ops import (
    RecordingParams,
    RecordingResult,
    RecordingSummaryParams,
    RecordingSummaryResult,
    ResultFile,
)
from . import meta, remarks
from .models import Track
from .naming import is_default_name
from .setup import (
    FinalizeCancelled,
    LiveConfig,
    PreparedLive,
    finalize_live,
    recover_session,
    summarize_folder,
)

logger = logging.getLogger(__name__)


def _cancelled(exc: MediaError) -> bool:
    """The job was cancelled: the cancel text in either interface language."""
    return str(exc) in (CANCEL_MESSAGE, catalog().get(CANCEL_MESSAGE))


def _cancel_error(exc: FinalizeCancelled) -> MediaError:
    """The cancel error exactly as the job raised it, so the job sees its own text."""
    cause = exc.__cause__
    return cause if isinstance(cause, MediaError) else MediaError(CANCEL_MESSAGE)


def _progress(on: Progress) -> Progress:
    furthest = 0.0

    def report(fraction: float | None, message: str) -> None:
        nonlocal furthest
        if fraction is not None:
            fraction = furthest = max(furthest, min(1.0, fraction))
        try:
            on(fraction, message)
        except MediaError as exc:
            if _cancelled(exc):
                raise FinalizeCancelled from exc
            raise

    return report


def _prepare(base: Path, params: RecordingParams | RecordingSummaryParams) -> None:
    base.mkdir()
    meta.set_started(base, params.started.isoformat())


def _silent(run: meta.Run, params: RecordingParams) -> list[remarks.Remark]:
    """The server decides a track is silent (Run.silent of the pipeline); silence alone is no
    remark. The phone's reason "blocked" on a track the server found silent is: a call went on
    and the other side was not recorded."""
    note = remarks.other_side_blocked()
    if (
        params.silent_reason.get("system") != "blocked"
        or "system" not in run.silent
        or note in run.notes
    ):
        return run.notes
    return [note, *run.notes]


def _result(
    base: Path,
    params: RecordingParams | RecordingSummaryParams,
    node: str,
    config: LiveConfig,
    *,
    failed: bool = False,
) -> dict:
    summary_only = isinstance(params, RecordingSummaryParams) and not params.finalize
    run = meta.last(base)
    if run is None or (failed and (summary_only or run.status != "failed")):
        previous = run
        run = meta.begin(config)
        run.kind = "summary" if summary_only else "process"
        if summary_only and previous is not None:
            run.notes = [
                r
                for r in previous.notes
                if r.step not in (remarks.Step.TITLE, remarks.Step.SUMMARY)
            ]
            run.info = previous.info
    if failed:
        run.status, run.finished = "failed", meta.now()
        run.error = _("Could not process the recording")
        run.notes.append(
            remarks.Remark(step=remarks.Step.SERVER, cause=run.error, hint=_("Process it again"))
        )
    run.settings["node"] = node
    files: dict[ResultFile, str | None] = {}
    transcript = base / ResultFile.TRANSCRIPT
    text = transcript.read_text(encoding="utf-8") if not failed and transcript.is_file() else None
    summary = base / ResultFile.SUMMARY
    if not failed and summary_only and params.summary and not summary.is_file():
        # The model may refuse without an exception: the pipeline keeps the reason in notes.
        # Return nothing to replace, so the client's old summary stays intact.
        failed = True
        run.error = next(
            (note.cause for note in run.notes if note.step == remarks.Step.SUMMARY),
            _("Could not write the summary"),
        )
    elif (
        not failed
        and not summary_only
        and not (text or "").strip()
        and run.tracks
        and all(track.error for track in run.tracks.values())
    ):
        # An empty successful ASR answer (silence) and every track failing are different results.
        failed = True
        run.error = _("Could not transcribe the recording")
    elif (
        not failed
        and isinstance(params, RecordingParams)
        and not params.partial
        and any(track.error for track in run.tracks.values())
    ):
        # Half a conversation must not replace the client's transcript, summary and title.
        failed = True
        run.error = _("Could not transcribe the recording")
        new_effect = _("The new transcript was not saved")
        run.notes = [
            note.model_copy(update={"effect": new_effect})
            if note.step == remarks.Step.ASR and note.effect
            else note
            for note in run.notes
            if note.step not in (remarks.Step.TITLE, remarks.Step.SUMMARY, remarks.Step.TRANSLATE)
        ]
    if failed:
        run.status, run.finished = "failed", meta.now()
    if not failed and text is not None:
        if isinstance(params, RecordingParams):
            files[ResultFile.TRANSCRIPT] = text
        files[ResultFile.SUMMARY] = (
            summary.read_text(encoding="utf-8") if summary.is_file() else None
        )
    translation = base / ResultFile.TRANSLATION
    if not failed and params.translate and translation.is_file():
        files[ResultFile.TRANSLATION] = translation.read_text(encoding="utf-8")
    if not summary_only:
        if isinstance(params, RecordingParams):
            run.part = params.part.model_dump() if params.part else None
            run.notes = _silent(run, params)
        # a run that asks the model for nothing (the Mac asks the server for it) needs none
        modelled = params.summary or params.retitle is not False or params.translate
        if text and not failed and modelled and not LLMSettings().cloud:
            run.notes.append(remarks.node_no_model())
        if params.recording_error and not any(r.step == remarks.Step.RECORD for r in run.notes):
            run.notes.insert(0, remarks.recording_failed(params.recording_error))
    run.remarks = [r.text for r in remarks.merged(run.notes)]
    changed = not failed and base.name != params.name
    model = (
        RecordingSummaryResult if isinstance(params, RecordingSummaryParams) else RecordingResult
    )
    return model(
        files=files, folder=base.name if changed else None, run=run.model_dump(mode="json")
    ).model_dump(mode="json")


def _place(params: RecordingParams | RecordingSummaryParams) -> remarks.Place:
    return remarks.Place(tz=params.started.tzinfo)


def process_recording(root: Path, values: dict, on: Progress) -> dict:
    params = RecordingParams.model_validate(values)
    config = LiveConfig(
        lang=params.lang,
        asr=params.asr,
        summary=params.summary,
        translate=params.translate,
        summary_lang=params.summary_lang,
        summary_hint=params.summary_hint,
        runner=None,
        mix_audio=False,
    )
    node = node_settings.name()
    work = root / ".mnema/work"
    work.mkdir(parents=True, exist_ok=True)
    with (
        TemporaryDirectory(dir=work) as temporary,
        remarks.at_place(_place(params)),
    ):
        base = Path(temporary) / params.name
        progress = _progress(on)
        try:
            _prepare(base, params)
            # Preparing the audio is quick and no step of the card: its message only, so the
            # card's steps (FINALIZE_STEPS) keep the whole bar.
            for label, path in params.tracks.items():
                audio.extract_audio(
                    Path(path),
                    base / f"{label}.wav",
                    on=lambda _f, message: progress(None, message),
                )
            result, _full = recover_session(
                base,
                config,
                retitle=params.retitle,
                user_title=lambda: params.user_title,
                recording_error=params.recording_error,
                on=progress,
            )
            return _result(result.base or base, params, node, config)
        except FinalizeCancelled as exc:
            raise _cancel_error(exc) from None
        except Exception as exc:  # noqa: BLE001 - the result keeps the history of a failed run
            logger.exception("could not process the recording on the node")
            return _result(getattr(exc, "mnema_base", base), params, node, config, failed=True)


def summarize_recording(path: Path, values: dict, on: Progress) -> dict:
    params = RecordingSummaryParams.model_validate(values)
    config = LiveConfig(
        lang=params.lang,
        summary=params.summary,
        translate=params.translate,
        summary_lang=params.summary_lang,
        summary_hint=params.summary_hint,
        runner=None,
        record=False,
        mix_audio=False,
    )
    node = node_settings.name()
    with (
        TemporaryDirectory(prefix="mnema-") as temporary,
        remarks.at_place(_place(params)),
    ):
        base = Path(temporary) / params.name

        def moved(path: Path) -> None:
            nonlocal base
            base = path

        try:
            _prepare(base, params)
            (base / "transcript.txt").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            if params.past is not None:
                saved = json.loads((base / meta.META_FILE).read_text(encoding="utf-8"))
                saved["runs"] = [params.past]
                (base / meta.META_FILE).write_text(
                    json.dumps(saved, ensure_ascii=False), encoding="utf-8"
                )
            if params.finalize:
                from .translate import LiveDialog, make_live_translator

                dialog = (
                    LiveDialog(params.translate, make_translator=make_live_translator)
                    if params.translate
                    else None
                )
                prepared = PreparedLive(
                    config=config,
                    base=base,
                    sources=[],
                    writers={},
                    dialog=dialog,
                    session_dialog=None,
                    transcribers={},
                    lang=params.lang,
                    status="",
                )
                prepared.recording_error = params.recording_error
                text = path.read_text(encoding="utf-8")
                tracks = [Track(label="mic", committed=[Segment(start=0, end=0, text=text)])]
                result, _full = finalize_live(
                    prepared,
                    tracks,
                    retitle=is_default_name(base) if params.retitle is None else params.retitle,
                    user_title=lambda: params.user_title,
                    on=_progress(on),
                    on_base=moved,
                )
            else:
                result = summarize_folder(
                    base,
                    lang=params.summary_lang,
                    hint=params.summary_hint,
                    summary=params.summary,
                    retitle=params.retitle,
                    translate=params.translate,
                    on=_progress(on),
                    on_base=moved,
                )
            return _result(result.base or base, params, node, config)
        except FinalizeCancelled as exc:
            raise _cancel_error(exc) from None
        except Exception as exc:  # noqa: BLE001 - a summary failure must not erase client files
            logger.exception("could not write the summary on the node")
            return _result(getattr(exc, "mnema_base", base), params, node, config, failed=True)
