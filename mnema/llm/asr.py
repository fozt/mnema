"""Cloud speech recognition (Groq, OpenAI): a WAV track → Transcript.

The track is cut at pauses (silero-vad, as for GigaAM): up to 5 minutes for whisper models, which
return timed segments themselves (verbose_json); up to 30 s for models without segment times
(gpt-4o-*), where a chunk is one segment with its own time. Times are shifted to the chunk start,
so a two-track dialogue is assembled by time as with local engines. A chunk is a WAV of the same
track (5 min at 16 kHz ≈ 9.6 MB, the limit is 25 MB), requests go one at a time (Groq allows 20 a
minute); limits, overload and dropped connections are retried as for text models. The key is the
selected model's provider's (LLMSettings.asr_key: MNEMA_API_KEY of that provider, otherwise
MNEMA_ASR_API_KEY); another provider's key is never substituted.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.base import MediaError, Progress, noop
from ..core.models import Segment, Transcript
from ..i18n import _

if TYPE_CHECKING:
    from .providers import Provider

_SR = 16000  # Mnema tracks are PCM16 mono 16 kHz
_LONG_S = 300.0  # whisper model chunk
_SHORT_S = 30.0  # chunk for models without segment times
_SHORT_GAP_S = 1.5  # for them: a pause this short keeps one segment


def provider_key(provider: Provider) -> str | None:
    """The provider's recognition key on this machine, or None."""
    from .config import LLMSettings

    return LLMSettings().asr_key(provider)


def no_key(provider: Provider) -> str:
    from .providers import TITLES

    return _("No {provider} key. Choose another recognition model", provider=TITLES[provider])


def credentials(choice: str) -> tuple[str, str, str | None]:
    """ "groq/whisper-large-v3" → (litellm name, Groq key, address): the selected model's
    provider key; without it a MediaError "No Groq key…", another provider is never used."""
    from . import providers

    found = providers.provider_of(choice)
    if found is None or found[0] not in providers.ASR_PROVIDERS:
        raise MediaError(_("Unknown recognition model: {choice}", choice=choice))
    provider, _model = found
    secret = provider_key(provider)
    if secret is None:
        raise MediaError(no_key(provider))
    return choice, secret, providers.litellm_base(provider)


def _timed(model: str) -> bool:
    """Whether the model returns timed segments: whisper does, gpt-4o-* only text."""
    return "whisper" in model


def chunks(speech: list[dict], *, timed: bool) -> list[tuple[int, int]]:
    """Request chunks (samples) from VAD speech: up to 5 minutes in a row for whisper models,
    otherwise up to 30 s with pauses of at most 1.5 s (a chunk is one segment)."""
    from ..core.backends import _group_vad_windows

    if timed:
        return _group_vad_windows(speech, int(_LONG_S * _SR), max_gap=10**12)
    return _group_vad_windows(speech, int(_SHORT_S * _SR), max_gap=int(_SHORT_GAP_S * _SR))


def _wav_bytes(wav: Path, start: int, end: int) -> bytes:
    """Samples [start, end) of the track as a separate in-memory WAV."""
    with wave.open(str(wav)) as src:
        src.setpos(start)
        frames = src.readframes(end - start)
        params = src.getparams()
    out = io.BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setparams(params)
        dst.writeframes(frames)
    return out.getvalue()


def segments(answer: Any, start: float, end: float, *, timed: bool) -> list[Segment]:
    """The model's answer for chunk [start, end) s → segments in recording time. Without timed
    segments the whole chunk is one segment."""
    found = (answer.get("segments") if timed else None) or []
    out = []
    for item in found:
        get = item.get if isinstance(item, dict) else lambda k, item=item: getattr(item, k, None)
        text = str(get("text") or "").strip()
        if text:
            s, e = float(get("start") or 0), float(get("end") or 0)
            out.append(Segment(start=start + s, end=min(end, start + max(e, s)), text=text))
    if out or found:
        return out
    text = str(answer.get("text") or "").strip()
    return [Segment(start=start, end=end, text=text)] if text else []


def _ask(model: str, key: str, base: str | None, data: bytes, lang: str | None, on: Progress):
    """One chunk, one request; limits, overload and drops are retried as for text models."""
    import litellm

    from .claude import _pause, _retry_wait

    litellm.suppress_debug_info = True
    retries = 0
    while True:
        try:
            return litellm.transcription(
                model=model, file=("chunk.wav", data), language=lang, api_key=key,
                api_base=base, response_format="verbose_json" if _timed(model) else "json",
            )  # fmt: skip
        except Exception as e:
            wait = _retry_wait(e, retries, live=False, started=False)
            if wait is None:
                raise
            retries += 1
            _pause(wait, on)


def transcribe(
    wav: Path, choice: str, *, lang: str | None = None, on: Progress = noop
) -> Transcript:
    """Cloud ASR in a worker process: the key is checked here and passed through the pipe (not
    the process arguments); the VAD memory goes away with the worker."""
    _model, key, base = credentials(choice)  # no key: a clear error before the process starts
    from ..core.settings import requested_asr_threads
    from ..core.worker import run

    result = run(
        "cloud_asr",
        {
            "wav": str(wav.resolve()),
            "choice": choice,
            "key": key,
            "base": base,
            "lang": lang,
            "threads": requested_asr_threads(),
        },
        on=on,
    )
    return Transcript.model_validate(result)


def _transcribe(
    wav: Path,
    choice: str,
    *,
    key: str,
    base: str | None = None,
    lang: str | None = None,
    on: Progress = noop,
) -> Transcript:
    """The WAV track by model choice ("groq/whisper-large-v3-turbo") with key, chunk by chunk;
    segments in recording time. If the model fails after retries the exception propagates (a
    remark on the track)."""
    model = choice
    from ..core.backends import _GIGAAM_LOCK, _read_wav_tensor, _speech_timestamps

    timed = _timed(model)
    on(None, _("Looking for speech…"))
    with _GIGAAM_LOCK:
        speech = _speech_timestamps(
            _read_wav_tensor(wav), max_speech_duration_s=_LONG_S if timed else _SHORT_S
        )
    pieces = chunks(speech, timed=timed)
    found: list[Segment] = []
    for i, (s, e) in enumerate(pieces, 1):
        on((i - 1) / len(pieces), _("Recognizing…"))
        answer = _ask(model, key, base, _wav_bytes(wav, s, e), lang, on)
        found += segments(answer, s / _SR, e / _SR, timed=timed)
    on(1.0, "")
    text = " ".join(x.text for x in found)
    return Transcript(text=text, segments=found, language=lang, engine=model)
