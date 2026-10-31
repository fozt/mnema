"""Live sentence-by-sentence translation with a sliding context (DeepL with the user's DeepL
key, else the model).

Only the newly committed sentence is translated, with the last N lines as context (terminology,
pronouns, coherence); earlier lines are never rewritten, so nothing flickers. The context holds
the earlier lines together with their translations ("line ⟶ translation" pairs), so a new line
continues what was already translated instead of starting from scratch (otherwise fragments
like "So..." come out incoherent). The final full-text translation is claude.translate.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

import httpx

from ..core.base import MediaError, noop
from ..i18n import _
from ..llm import claude, providers
from ..llm.config import LLMSettings, deepl_key

logger = logging.getLogger(__name__)

# transient DeepL answers: a retry helps (unlike 400/403/456)
_DEEPL_TRANSIENT = {429, 500, 502, 503, 504, 529}

# the model prompt stays as it is: prompts are tuned separately
_SYSTEM = (
    "Ты синхронный переводчик живой речи на язык: {lang}. Тебе дают ПОСЛЕДНЮЮ реплику и, для "
    "контекста, несколько предыдущих. Переведи ТОЛЬКО последнюю реплику, естественно и связно, "
    "сохраняя терминологию и имена из контекста. У прошлых реплик после «⟶» показан их уже "
    "сделанный перевод — переводи новую так, чтобы она читалась как продолжение уже "
    "переведённого (согласуй термины, местоимения и начатые конструкции). Реплики — сырой "
    "вывод распознавания речи: обрывки, оговорки и смешение языков переводи как есть, не "
    "достраивая смысл. Отвечай строго переводом, без пояснений, комментариев и просьб "
    "уточнить; непереводимое — пустой ответ.\n"
    "{context}"
)

# interjections and acknowledgements: translating them is noise and queue delay
_FILLER = re.compile(
    r"^(?:(?:m+h?m*|hm+|uh+|um+|uh[- ]huh|ok(?:ay)?|yeah|yep|м+|мм+|хм+|э+|ага|угу)"
    r"[\s.!?,…-]*)+$",
    re.IGNORECASE,
)


def worth_translating(text: str) -> bool:
    """Whether a line is worth translating: lines without letters and interjections are not.

    Saves the translation queue (a third of a call's lines are "Mm-hmm", "Okay", "…") and gives
    the model no excuse for meta answers ("send me the text to translate") on untranslatable input.
    """
    if not re.search(r"[^\W\d_]", text):  # no letters: "…", digits, punctuation
        return False
    return not _FILLER.match(text.strip())


# non-verbal recognition noise; unlike _FILLER, "okay/yeah/угу" are meaningful answers and stay;
# single "а"/"о"/"м" stay too, they can be words
_NOISE_RE = re.compile(
    r"(?<![\w-])(?:u+h+|u+m+|erm+|h+m+|m{2,}|mhm+|uh[- ]huh|mm[- ]hmm|o+h+|a+h+"
    r"|э(?:[-\s]?э)*|м(?:[-\s]м)+|х+м+)(?![\w-])[.!?,…]*",
    re.IGNORECASE,
)


def strip_noise(text: str) -> str:
    """Remove non-verbal recognition noise (uh, um, oh, э-э…) for the live view and translation.

    The transcript file keeps the raw text; only what goes to the screen and the translator is
    cleaned. Empty after cleaning means the line was pure noise (the caller does not show it).
    """
    out = _NOISE_RE.sub("", text)
    # runs of dots and ellipses (whisper emits "..." on pauses, adjacent segments join into
    # "......") become one "…"; sentence dots and "e.g." have letters between them and stay
    out = re.sub(r"[.…](?:\s*[.…])+", "…", out)
    out = re.sub(r"([,;:.!?…])\s*[,;:]+", r"\1", out)  # punctuation orphaned by the removal
    out = re.sub(r"\s{2,}", " ", out)
    return re.sub(r"^[\s,;:.!?…-]+", "", out).strip()


_PAIR_SEP = " ⟶ "  # separates "line ⟶ translation" in the context window


class PushTranslator(Protocol):
    """Live translator of one direction: push(line) → translation (see session.Translator)."""

    def push(self, sentence: str, on_partial: Callable[[str], None] | None = None) -> str: ...


class _ContextWindow:
    """Sliding window of the last lines with their translations ("line ⟶ translation").

    The window includes the current line (prev returns everything else). Translations run one
    at a time (one worker), so resolve always completes the last line.
    """

    def __init__(self, size: int) -> None:
        self._lines: deque[str] = deque(maxlen=max(size, 1))

    def add(self, sentence: str) -> None:
        self._lines.append(sentence)

    def prev(self) -> list[str]:
        """Earlier lines, without the current one (added last)."""
        return list(self._lines)[:-1]

    def resolve(self, sentence: str, translation: str) -> None:
        if translation and self._lines and self._lines[-1] == sentence:
            self._lines[-1] = f"{sentence}{_PAIR_SEP}{translation}"


class LiveTranslator:
    """Incremental LLM translator: push(sentence) → translation with a sliding context.

    context_lines is an external context provider (dialog mode: one window for both directions
    with speaker labels); without it, an own window of the last "line ⟶ translation" pairs.
    """

    def __init__(
        self,
        to_lang: str,
        *,
        context: int = 8,
        context_lines: Callable[[], list[str]] | None = None,
    ) -> None:
        self._to_lang = to_lang
        self._context_lines = context_lines
        self._window = _ContextWindow(context)

    def push(self, sentence: str, on_partial: Callable[[str], None] | None = None) -> str:
        """Translate a line; on_partial, if given, receives the growing translation (stream)."""
        sentence = sentence.strip()
        if not sentence:
            return ""
        if self._context_lines is not None:
            prev = self._context_lines()
        else:
            self._window.add(sentence)
            prev = self._window.prev()
        ctx = ("Контекст:\n" + "\n".join(prev)) if prev else ""
        system = _SYSTEM.format(lang=self._to_lang, context=ctx)
        # the model is resolved on use: the session is built without one
        creds = LLMSettings().credentials()
        out = claude._stream_text(
            system, sentence, creds=creds, max_tokens=2048, on=noop, on_text=on_partial
        )
        if self._context_lines is None:
            self._window.resolve(sentence, out)
        return out


# DeepL target_lang codes: en/pt without a region are deprecated, pick the variant explicitly
_DEEPL_TARGET = {"en": "EN-US", "pt": "PT-BR"}


def _deepl_url(key: str, path: str) -> str:
    """A key ending in ":fx" is the free plan (api-free.deepl.com), otherwise api.deepl.com."""
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    return f"https://{host}/v2/{path}"


def _deepl_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"DeepL-Auth-Key {key}"}


def check_deepl_key(key: str, *, timeout: float = 10.0) -> None:
    """The DeepL key works: GET /v2/usage. A refusal is a MediaError for the user."""
    from ..llm.redact import add_secret

    add_secret(key)
    if not key:
        raise MediaError(_("A key is required"))
    if not all(" " < c <= "~" for c in key):
        raise MediaError(_(providers.BAD_KEY))
    try:
        with providers.http(timeout) as client:
            resp = client.get(_deepl_url(key, "usage"), headers=_deepl_headers(key))
    except httpx.HTTPError as e:
        raise MediaError(_("DeepL is not responding, try again later")) from e
    if resp.status_code in (401, 403):
        raise MediaError(_("DeepL did not accept the key"))
    if resp.is_error:
        raise MediaError(_("DeepL answered with error {code}", code=resp.status_code))


def deepl_reason(exc: httpx.HTTPError) -> str:
    """Why a DeepL request failed, in the user's words: the key, the character limit, an error
    code or no answer."""
    code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    if code in (401, 403):
        return _("DeepL did not accept the key")
    if code == 456:
        return _("the DeepL character limit is used up")
    if code is None or code in _DEEPL_TRANSIENT:
        return _("DeepL did not answer")
    return _("DeepL answered with error {code}", code=code)


class DeepLTranslator:
    """Live translator over the DeepL API: does not stream (on_partial is ignored) but answers
    fast (~0.3–0.7 s per phrase). Plain httpx, no deepl package.

    The context is the same "line ⟶ translation" pairs in the context parameter (DeepL uses it
    but does not translate it). An error or timeout raises: the session worker commits the line
    without a translation and the session goes on.
    """

    def __init__(
        self,
        to_lang: str,
        *,
        api_key: str,
        context: int = 8,
        context_lines: Callable[[], list[str]] | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._key = api_key
        self._url = _deepl_url(api_key, "translate")
        low = to_lang.lower()
        self._target = _DEEPL_TARGET.get(low, low.upper())
        self._timeout = timeout
        self._context_lines = context_lines
        self._window = _ContextWindow(context)

    def _request(self, payload: dict) -> str:
        with providers.http(self._timeout) as client:
            resp = client.post(self._url, json=payload, headers=_deepl_headers(self._key))
        resp.raise_for_status()
        return str(resp.json()["translations"][0]["text"]).strip()

    def push(self, sentence: str, on_partial: Callable[[str], None] | None = None) -> str:
        sentence = sentence.strip()
        if not sentence:
            return ""
        if self._context_lines is not None:
            prev = self._context_lines()
        else:
            self._window.add(sentence)
            prev = self._window.prev()
        payload: dict = {"text": [sentence], "target_lang": self._target}
        if prev:
            payload["context"] = "\n".join(prev)
        try:
            out = self._request(payload)
        except (httpx.TransportError, httpx.TimeoutException):
            time.sleep(0.4)  # transient network failure: one quick retry
            out = self._request(payload)
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in _DEEPL_TRANSIENT:
                raise  # 400/403/456 (quota, key): a retry will not help, the model fallback will
            time.sleep(0.4)
            out = self._request(payload)
        if self._context_lines is None:
            self._window.resolve(sentence, out)
        return out


class _FallbackTranslator:
    """DeepL with a model fallback: when DeepL fails even after a retry, the phrase is
    translated by the model instead of staying untranslated. The model translator is created on
    the first failure, so the DeepL path never touches the model."""

    def __init__(
        self, primary: PushTranslator, make_fallback: Callable[[], PushTranslator]
    ) -> None:
        self._primary = primary
        self._make_fallback = make_fallback
        self._fallback: PushTranslator | None = None

    def push(self, sentence: str, on_partial: Callable[[str], None] | None = None) -> str:
        try:
            return self._primary.push(sentence, on_partial)
        except Exception:  # noqa: BLE001 - DeepL did not answer even after a retry
            logger.warning(
                "DeepL did not answer, translating the phrase with the model", exc_info=True
            )
            if self._fallback is None:
                self._fallback = self._make_fallback()
            return self._fallback.push(sentence, on_partial)


def make_live_translator(
    to_lang: str, context_lines: Callable[[], list[str]] | None = None
) -> PushTranslator:
    """Live translator of one direction: DeepL with the user's DeepL key (faster and cheaper),
    else the model. With a model configured, DeepL falls back to it instead of losing the
    phrase."""
    key = deepl_key()
    if key:
        deepl = DeepLTranslator(to_lang, api_key=key, context_lines=context_lines)
        if claude.llm_available():
            return _FallbackTranslator(
                deepl, lambda: LiveTranslator(to_lang, context_lines=context_lines)
            )
        return deepl
    return LiveTranslator(to_lang, context_lines=context_lines)


def live_translate_available() -> bool:
    """Whether live translation is possible: the user's DeepL key or a configured model."""
    return bool(deepl_key()) or claude.llm_available()


def detect_lang(text: str) -> str:
    """Rough phrase language: mostly Cyrillic → "ru", else "en".

    Enough for a ru↔en dialog and more reliable than ASR metadata; re-exports claude._detect_lang.
    """
    return claude._detect_lang(text)


# detection is binary (ru/en), so the other side's language is the opposite of mine
_OTHER = {"ru": "en", "en": "ru"}


# translator factory of one direction: (language, context provider) → translator
TranslatorFactory = Callable[[str, Callable[[], list[str]]], PushTranslator]


class _DialogRoute:
    """Translator of one direction that writes the finished translation into the shared window.

    The translation worker calls push after route, so by the time the next lines are translated
    the window already holds the "line ⟶ translation" pair for both directions.
    """

    def __init__(self, dialog: LiveDialog, label: str, translator: PushTranslator) -> None:
        self._dialog = dialog
        self._label = label
        self._translator = translator

    def push(self, sentence: str, on_partial: Callable[[str], None] | None = None) -> str:
        out = self._translator.push(sentence, on_partial)
        self._dialog._record(self._label, sentence, out)
        return out


class LiveDialog:
    """Two-way live translation: direction and speaker label follow the phrase language.

    Only my language (my_lang) is given. A phrase in my language gets the label «я» and is
    translated into the other side's language (I read it aloud); any other phrase gets «они» and
    is translated into mine. The labels are context for the model, not interface text. The
    context is shared by both directions, so the translator sees the whole dialog.
    make_translator builds the translator of each direction (DeepL or the model; the model by
    default).
    """

    def __init__(
        self,
        my_lang: str = "ru",
        *,
        their_lang: str | None = None,
        make_translator: TranslatorFactory | None = None,
    ) -> None:
        self.my_lang = my_lang
        self.their_lang = their_lang or _OTHER.get(my_lang, "en")
        self._lines: deque[str] = deque(maxlen=8)  # shared window: "я: …" / "они: …"
        self._lock = threading.Lock()  # route runs on the main thread, push/_record on the worker
        make: TranslatorFactory = make_translator or (
            lambda lang, ctx: LiveTranslator(lang, context_lines=ctx)
        )
        self._to_my = make(self.my_lang, self._context)
        self._to_their = make(self.their_lang, self._context)

    def _context(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def _record(self, label: str, sentence: str, translation: str) -> None:
        """Attach a finished translation to its line, searching from the end: the main thread
        may have added newer lines meanwhile."""
        if not translation:
            return
        line = f"{label}: {sentence}"
        with self._lock:
            for i in range(len(self._lines) - 1, -1, -1):
                if self._lines[i] == line:
                    self._lines[i] = f"{line}{_PAIR_SEP}{translation}"
                    return

    def route(self, sentence: str) -> tuple[str, PushTranslator]:
        """Phrase → (speaker label, translator of the right direction), by the phrase language."""
        mine = detect_lang(sentence) == self.my_lang
        return self.route_as("я" if mine else "они", sentence)

    def route_as(self, label: str, sentence: str) -> tuple[str, PushTranslator]:
        """Route with a known speaker label (by source: mic = «я», system = «они»); the
        direction still follows the phrase language.

        The direction cannot be pinned to the source: on an English call my English phrase must
        be translated into my language, not into the other side's (en→en).
        """
        with self._lock:
            self._lines.append(f"{label}: {sentence}")
        mine = detect_lang(sentence) == self.my_lang
        base = self._to_their if mine else self._to_my
        return label, _DialogRoute(self, label, base)


class SpeakerDialog(Protocol):
    def route_as(self, label: str, sentence: str) -> tuple[str, PushTranslator]: ...


class PinnedDialog:
    """Dialog with the speaker label pinned by source (mic → «я», system → «они»); the
    direction follows the phrase language (see route_as). Context and translators are shared
    with the parent LiveDialog, so the window sees both sides whichever source brought them.
    """

    def __init__(self, dialog: SpeakerDialog, label: str) -> None:
        self._dialog = dialog
        self.label = label

    def route(self, sentence: str) -> tuple[str, PushTranslator]:
        return self._dialog.route_as(self.label, sentence)
