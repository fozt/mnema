"""Remarks after processing a recording, in the user's words.

A remark says what happened (cause), what it means for the result (effect), what to do (hint)
and, when needed, a command (printed on its own line so it can be copied; terminal only), plus a
short cause and effect for the menu. The full text goes to the terminal and to the app's
"Details"; the menu packs up to three lines from short/short_effect. All wording lives here: the
core and the model layer hand over data (llm/notes.LightNote), not phrases. Remarks are stored in
the history and reach the phone in the language of the processing, so hints name the machine
where the fix belongs (_where) and carry no commands.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..core import base
from ..core.base import MONTHS as MONTHS  # re-export until bar/panel uses i18n.month_short
from ..i18n import _, month_short
from ..llm.errors import ErrorKind, classify, human_reason
from ..llm.notes import Cloud, LightNote


class _Translated(Mapping[str, str]):
    """English source strings by key, read in the current language."""

    def __init__(self, texts: dict[str, str]) -> None:
        self._texts = texts

    def __getitem__(self, key: str) -> str:
        return _(self._texts[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._texts)

    def __len__(self) -> int:
        return len(self._texts)


SOURCE_NAMES: Mapping[str, str] = _Translated({"mic": "Microphone", "system": "System audio"})


class Place(BaseModel):
    """Where the processing runs: the server name its owner gave (empty: none) and the
    recording's time zone."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    node: str = ""
    tz: tzinfo | None = None


PLACE: ContextVar[Place] = ContextVar("remarks_place", default=Place())  # noqa: B039 - immutable


@contextmanager
def at_place(place: Place) -> Iterator[None]:
    token = PLACE.set(place)
    try:
        yield
    finally:
        PLACE.reset(token)


def _where() -> str:
    """Where to fix the model, readable on the phone too: every user sets her model and keys in
    the app on the Mac, the server's ones too."""
    return _("in Mnema settings on the Mac")


class Step(StrEnum):
    RECORD = "record"
    SERVER = "server"
    ASR = "asr"
    SUMMARY = "summary"
    TITLE = "title"
    TRANSLATE = "translate"
    MIX = "mix"


class Remark(BaseModel):
    step: Step
    cause: str  # what happened and why, one or two sentences
    effect: str = ""  # what it means for the result
    hint: str = ""  # what to do
    command: str = ""  # a command for it, printed on its own line as is
    short: str = ""  # cause for the menu, up to ~45 characters (empty: not shown as a cause)
    short_effect: str = ""  # effect for the menu, lowercase: "no title"
    code: str = ""  # remark kind for a client that finds it without the text (the phone)

    @property
    def text(self) -> str:
        body = " ".join(_period(p) for p in (self.cause, self.effect, self.hint) if p)
        return f"{body}\n{self.command}" if self.command else body


def _period(text: str) -> str:
    text = text.strip()
    text = text[:1].upper() + text[1:]
    return text if text.endswith((".", "!", "?", "…", ":")) else text + "."


def until_text(until: datetime, now: datetime, *, dated: bool = False) -> str:
    """When a limit resets, in local time: within 24 hours just the time; later, and in remarks
    that are stored (dated), with the date."""
    t = until.astimezone(PLACE.get().tz)
    time = f"{t:%H:%M}"
    if not dated and until - now < timedelta(hours=24):
        return _("until {time}", time=time)
    return _("until {month} {day}, {time}", month=month_short(t.month), day=t.day, time=time)


def _source(label: str) -> str:
    return SOURCE_NAMES.get(label, label)


def plain_error(reason: str, limit: int = 160) -> str:
    """A failure reason in the user's words, first line only: no ffmpeg stderr tail, tracebacks
    or internal words ("runner", "HTTP 502: Bad Gateway")."""
    text = reason.strip()
    low = text.lower()
    gateway = ("перезапускается или недоступен", "restarting or unavailable")
    if re.search(r"http 50[234]", low) or any(g in low for g in gateway):
        return _("the server is restarting or unavailable")
    line = (text.splitlines() or [""])[0].strip().rstrip(".").replace("раннер", "сервер")
    line = re.sub(r"(?<![-\w])runner", "server", line)  # not the --runner option
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


# ── models: one active model, no fallback ─────────────────────────────────────

# OTHER takes cloud_detail
_KIND_REASONS = {
    ErrorKind.INVALID_KEY: "the model key was rejected",
    ErrorKind.DISABLED: "the model key is disabled",
    ErrorKind.NO_MONEY: "the account is out of money",
    ErrorKind.NO_MODEL: "no access to the model",
    ErrorKind.RATE_LIMIT: "request limit exceeded",
    ErrorKind.DAILY_LIMIT: "daily request limit used up",
    ErrorKind.OVERLOADED: "the model service is not responding",
    ErrorKind.NETWORK: "no connection",
    ErrorKind.REGION: "the model service does not accept requests from your country or network",
    ErrorKind.PRIVACY: "the model is blocked by OpenRouter privacy settings",
}
_KIND_SHORT = {
    ErrorKind.INVALID_KEY: "Model key rejected",
    ErrorKind.DISABLED: "Model key disabled",
    ErrorKind.NO_MONEY: "Account out of money",
    ErrorKind.NO_MODEL: "Model unavailable",
    ErrorKind.RATE_LIMIT: "Model request limit",
    ErrorKind.DAILY_LIMIT: "Model daily limit",
    ErrorKind.OVERLOADED: "Model service not responding",
    ErrorKind.NETWORK: "No connection to the model",
    ErrorKind.REGION: "Model unavailable in your country",
    ErrorKind.PRIVACY: "Model blocked by privacy settings",
}
# {where} is _where()
_HINTS = {
    ErrorKind.INVALID_KEY: "Replace the model key {where}",
    ErrorKind.DISABLED: "Replace the model key {where}",
    ErrorKind.NO_MONEY: "Top up your account with the model provider",
    ErrorKind.NO_MODEL: "Choose another model {where}",
    ErrorKind.PRIVACY: "Allow the model in the privacy settings: openrouter.ai/settings/privacy",
    ErrorKind.REGION: "Choose another provider {where}",
}
_NO_MODEL = "No model configured"
_SET_MODEL_HINT = "Set up a model {where}"
_RETRY_HINT = "When the model works again, choose “Summarize” in the recording menu"
_SUMMARIZE_HINT = "Choose “Summarize” in the recording menu"
_NO_ANSWER = "The model did not respond"


def _hint(kind: ErrorKind | None) -> str:
    return _(_HINTS[kind], where=_where()) if kind in _HINTS else ""


def _set_model_hint() -> str:
    return _(_SET_MODEL_HINT, where=_where())


def _model_cause(note: LightNote) -> tuple[str, str, str]:
    """(cause, short, hint) of the active model's refusal; one function for the title and the
    summary so that merged joins them into one remark."""
    if note.cloud == Cloud.NOT_SET:
        return _(_NO_MODEL), _(_NO_MODEL), _set_model_hint()
    if note.kind in _KIND_REASONS:
        reason = _(_KIND_REASONS[note.kind])
    else:
        reason = plain_error(note.cloud_detail) if note.cloud_detail else _("unknown reason")
    if note.until is not None and note.kind in (ErrorKind.RATE_LIMIT, ErrorKind.DAILY_LIMIT):
        reason = f"{reason} {until_text(note.until, datetime.now().astimezone(), dated=True)}"
    if note.model:
        cause = _("Model {model} did not respond: {reason}", model=note.model, reason=reason)
    else:
        cause = _("The model did not respond: {reason}", reason=reason)
    short = _(_KIND_SHORT[note.kind]) if note.kind in _KIND_SHORT else _(_NO_ANSWER)
    return cause, short, _hint(note.kind)


_NO_SUMMARY = "There is no summary"
_NO_SUMMARY_SHORT = "no summary"
_NO_TITLE = "The recording is named by its start time"
_NO_TITLE_SHORT = "no title"
_NO_MODEL_EFFECTS = {
    Step.TITLE: (_NO_TITLE, _NO_TITLE_SHORT),
    Step.SUMMARY: (_NO_SUMMARY, _NO_SUMMARY_SHORT),
}
_MODEL_STEPS = {Step.TITLE, Step.SUMMARY}


def light(step: Step, note: LightNote | None) -> Remark | None:
    """Why the active model made no title or summary and what to do; None — it did."""
    if note is None:
        return None
    cause, short, hint = _model_cause(note)
    effect, short_effect = _NO_MODEL_EFFECTS[step]
    return Remark(
        step=step,
        cause=cause,
        effect=_(effect),
        hint=_join_hints([hint, _(_RETRY_HINT)]),
        short=short,
        short_effect=_(short_effect),
    )


def _join_hints(hints: list[str]) -> str:
    """Hints joined with a period, without repeats; one that introduces a command ("…:") goes
    last, since the command is printed right after it."""
    unique = [h for h in dict.fromkeys(h.strip() for h in hints) if h]
    return ". ".join(sorted(unique, key=lambda h: h.endswith(":")))


# The limit reset time inside a cause, in either language: it differs between steps by minutes.
_UNTIL = re.compile(r" (?:до \d{1,2} \S+|until \S+ \d{1,2},) \d{2}:\d{2}")


def _same_cause(a: Remark, b: Remark) -> bool:
    if a.step not in _MODEL_STEPS or b.step not in _MODEL_STEPS:
        return False
    return a.cause == b.cause or _UNTIL.sub("", a.cause) == _UNTIL.sub("", b.cause)


def merged(remarks: list[Remark]) -> list[Remark]:
    """One cause behind several steps (the model refused both the title and the summary) becomes
    one remark: cause and hint once, effects in a row."""
    out: list[Remark] = []
    for r in remarks:
        same = next((o for o in out if _same_cause(o, r)), None)
        if same is None:
            out.append(r.model_copy())
            continue
        effects = [e for e in (same.effect, r.effect) if e]
        same.effect = ". ".join(dict.fromkeys(effects))
        same.hint = _join_hints([*same.hint.split(". "), *r.hint.split(". ")])
        same.command = same.command or r.command
        if r.short_effect and r.short_effect not in same.short_effect.split(", "):
            same.short_effect = ", ".join(e for e in (same.short_effect, r.short_effect) if e)
    return out


# ── recording and speech recognition ──────────────────────────────────────────

_FILES_NOT_SAVED = "the recording files were not saved completely"  # same as the phone helper


def files_not_saved() -> str:
    return _(_FILES_NOT_SAVED)


def track_stopped(label: str) -> str:
    """Why a recording is incomplete: "system audio stopped recording"."""
    if label == "mic":
        return _("the microphone stopped recording")
    if label == "system":
        return _("system audio stopped recording")
    return _("{source} stopped recording", source=label.lower())


def __getattr__(name: str) -> str:
    # Stopgap for callers not yet moved to files_not_saved(): read in the current language at
    # attribute access.
    if name == "FILES_NOT_SAVED":
        return files_not_saved()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_LIVE_KEPT = "The transcript keeps the draft text written for it during the call"
_NO_TRACK_SPEECH = "The transcript has no speech from this track"


def recording_failed(reason: str) -> Remark:
    return Remark(
        step=Step.RECORD,
        cause=_("Recording stopped because of an error: {reason}", reason=reason),
        effect=_("Only the recorded part of the conversation was saved and processed"),
        hint=_("Check free disk space and access to the recordings folder"),
        short=_("Recording incomplete"),
    )


def node_asr_reason(error: Exception) -> str:
    """Why recognition failed: a cloud refusal in the user's words, otherwise only where it
    failed (the engine's text may point to a terminal)."""
    if classify(error) is not ErrorKind.OTHER:
        return human_reason(error)
    return _("error on the Mac") if base.processed_here() else _("error on the server")


def asr_failed(label: str, reason: str | Exception, *, live_kept: bool) -> Remark:
    detail = node_asr_reason(reason if isinstance(reason, Exception) else RuntimeError(reason))
    source = _source(label)
    return Remark(
        step=Step.ASR,
        cause=_("Could not recognize the “{source}” track: {detail}", source=source, detail=detail),
        effect=_(_LIVE_KEPT) if live_kept else _(_NO_TRACK_SPEECH),
        hint=_("Once the cause is fixed, process the recording again"),
        short=_("Track “{source}” not recognized", source=source),
    )


OTHER_SIDE_SILENT = "other_side_silent"  # code: the other side was not recorded, cause known
_OTHER_SIDE_SILENT = {
    "blocked": "the phone did not provide the call audio",
    "none": "there was no call",
}


_SILENT_HINTS = {
    "system": "If the other side could be heard, check the permission to record system audio",
    "mic": "If you could be heard, check the microphone and the recording permission",
}


def track_silent(label: str, reason: Literal["blocked", "none"] | None = None) -> Remark:
    """reason for the other side: blocked — a call went on but no audio came, none — no call."""
    source = _source(label)
    short = _("No sound on the “{source}” track", source=source)
    if label == "system" and reason is not None:
        return Remark(
            step=Step.ASR,
            cause=_(
                "The “{source}” track has no sound: {reason}",
                source=source,
                reason=_(_OTHER_SIDE_SILENT[reason]),
            ),
            short=short,
            code=OTHER_SIDE_SILENT,
        )
    return Remark(
        step=Step.ASR,
        cause=_("The “{source}” track has no sound", source=source),
        hint=_(_SILENT_HINTS[label]) if label in _SILENT_HINTS else "",
        short=short,
    )


def node_no_model() -> Remark:
    return Remark(
        step=Step.SUMMARY,
        cause=_(_NO_MODEL),
        effect=_(_NO_SUMMARY),
        hint=_join_hints([_set_model_hint(), _(_RETRY_HINT)]),
        short=_(_NO_MODEL),
        short_effect=_(_NO_SUMMARY_SHORT),
    )


def asr_no_speech(label: str, *, live_kept: bool) -> Remark:
    """Recognition heard no speech where the draft had some (or all of it was silence): an
    empty track, not a failure."""
    source = _source(label)
    return Remark(
        step=Step.ASR,
        cause=_("No speech found on the “{source}” track", source=source),
        effect=_(_LIVE_KEPT) if live_kept else _(_NO_TRACK_SPEECH),
        short=_("No speech on the “{source}” track", source=source),
    )


# ── summary, title, translation, combined file, server ──────────────────────────


def summary_failed(reason: str) -> Remark:
    return Remark(
        step=Step.SUMMARY,
        cause=_("The summary was not written: {reason}", reason=plain_error(reason)),
        hint=_(_SUMMARIZE_HINT),
        short_effect=_(_NO_SUMMARY_SHORT),
    )


def summary_garbled() -> Remark:
    """The model answered the summary request, but no points could be parsed (the answer was cut
    off or in the wrong format)."""
    garbled = _("The model answered in the wrong format")
    return Remark(
        step=Step.SUMMARY,
        cause=garbled,
        effect=_(_NO_SUMMARY),
        hint=_(_SUMMARIZE_HINT),
        short=garbled,
        short_effect=_(_NO_SUMMARY_SHORT),
    )


def title_failed(reason: str) -> Remark:
    return Remark(
        step=Step.TITLE,
        cause=_("Could not come up with a title: {reason}", reason=plain_error(reason)),
        effect=_(_NO_TITLE),
        hint=_("Choose “Rename” in the recording menu"),
        short_effect=_(_NO_TITLE_SHORT),
    )


def translation_failed() -> Remark:
    """No command: the remark goes to the history and the phone; the CLI prints the command with
    a path on its own disk."""
    return Remark(
        step=Step.TRANSLATE,
        cause=_("The translation failed"),
        effect=_("There is no translation file"),
        short_effect=_("no translation"),
    )


def mix_failed(reason: str) -> Remark:
    return Remark(
        step=Step.MIX,
        cause=_(
            "The combined recording file was not created: {reason}", reason=plain_error(reason)
        ),
        effect=_("The tracks stay as separate files mic.wav and system.wav"),
        short_effect=_("no combined mp3"),
    )


def server_down(server: str, command: str = "") -> Remark:
    """The chosen server did not answer: the recording waits for it and never moves to another
    server or to this computer by itself. server — its name as the user sees it; command — the
    terminal command to finish it; the app has none, it offers "Retry" on the card."""
    unavailable = _("{server} is unavailable", server=server)
    return Remark(
        step=Step.SERVER,
        cause=unavailable,
        effect=_("The audio is saved"),
        hint=(
            _("Process the recording when the server is back:")
            if command
            else _("Choose “Retry” on the recording card")
        ),
        command=command,
        short=unavailable,
        short_effect=_("recording not processed"),
    )


def details(remarks: list[str], info: list[str]) -> str:
    """The "Details" of a processing: remarks in full (Remark.text or the stored history) and
    notes about the recording (part cut out, WAV header repaired, full recording in the Trash);
    empty when there is nothing to say."""
    parts = []
    if remarks:
        parts.append(_("Notes") + "\n\n" + "\n\n".join(f"- {r}" for r in remarks))
    if info:
        parts.append(_("About this recording") + "\n\n" + "\n\n".join(f"- {n}" for n in info))
    return "\n\n".join(parts) + "\n" if parts else ""


# ── menu: up to three short lines ─────────────────────────────────────────────

MENU_WIDTH = 58
_CAUSE_RANK = [
    Step.SERVER, Step.ASR, Step.SUMMARY, Step.TITLE, Step.MIX, Step.TRANSLATE,
]  # fmt: skip


def menu_lines(remarks: list[Remark]) -> list[str]:
    """Remarks → up to three menu lines: the main cause (by importance: server, recognition,
    models…), then the effects without repeats, packed into lines of up to MENU_WIDTH."""
    if not remarks:
        return []
    ranked = sorted(
        (r for r in remarks if r.short),
        key=lambda r: _CAUSE_RANK.index(r.step) if r.step in _CAUSE_RANK else len(_CAUSE_RANK),
    )
    lines = [ranked[0].short] if ranked else []
    row = ""
    effects = [e for r in remarks for e in r.short_effect.split(", ") if e]
    for effect in dict.fromkeys(effects):
        if row and len(row) + 2 + len(effect) > MENU_WIDTH:
            lines.append(row)
            row = effect
        else:
            row = f"{row}, {effect}" if row else effect
    if row:
        lines.append(row)
    lines = [(ln[:1].upper() + ln[1:])[:MENU_WIDTH] for ln in lines if ln][:3]
    return lines or [_("With notes")]
