"""Live sentence-by-sentence translation with a sliding context (by the configured model).

Only the newly committed sentence is translated, with the last N lines as context (terminology,
pronouns, coherence); earlier lines are never rewritten, so nothing flickers. The context holds
the earlier lines together with their translations ("line ⟶ translation" pairs), so a new line
continues what was already translated instead of starting from scratch (otherwise fragments
like "So..." come out incoherent). The final full-text translation is claude.translate.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from collections.abc import Callable
from typing import Protocol

from ..core.base import noop
from ..llm import claude
from ..llm.config import LLMSettings

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


def make_live_translator(
    to_lang: str, context_lines: Callable[[], list[str]] | None = None
) -> PushTranslator:
    """Live translator of one direction: the configured model."""
    return LiveTranslator(to_lang, context_lines=context_lines)


def live_translate_available() -> bool:
    """Whether live translation is possible: a model is configured."""
    return claude.llm_available()


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
    make_translator builds the translator of each direction (the model by default).
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
