"""Live translation through the selected node: each finished phrase is a separate request."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

import httpx

from ..client import MnemaClient
from ..core.base import MediaError
from ..i18n import _
from .translate import _OTHER, _PAIR_SEP, detect_lang

_CONTEXT = 3  # previous phrases per request


class _Phrase:
    def __init__(self, dialog: RemoteDialog, label: str) -> None:
        self._dialog, self._label = dialog, label

    def push(self, sentence: str, on_partial: Callable[[str], None] | None = None) -> str:
        return self._dialog._translate(self._label, sentence)


class RemoteDialog:
    """The context is the last phrases of both tracks with their translations; the node is
    stateless. The session worker translates one phrase at a time, so the window grows in order.
    """

    def __init__(self, client: MnemaClient, *, my_lang: str) -> None:
        self.my_lang = my_lang
        self.their_lang: str | None = None  # direction follows the phrase language
        self._client = client
        self._lines: deque[str] = deque(maxlen=_CONTEXT)

    def route(self, sentence: str) -> tuple[str, _Phrase]:
        # «я»/«они» are speaker labels of the model's context, not interface text
        return self.route_as("я" if detect_lang(sentence) == self.my_lang else "они", sentence)

    def route_as(self, label: str, sentence: str) -> tuple[str, _Phrase]:
        return label, _Phrase(self, label)

    def _translate(self, label: str, sentence: str) -> str:
        mine = detect_lang(sentence) == self.my_lang
        target = _OTHER.get(self.my_lang, "en") if mine else self.my_lang
        text = ""
        try:
            text = self._client.translate_phrase(
                sentence, context=list(self._lines), target_lang=target
            )
        except httpx.HTTPError as exc:
            raise MediaError(
                _("No connection to the server: the phrase was not translated")
            ) from exc
        finally:
            self._lines.append(f"{label}: {sentence}" + (f"{_PAIR_SEP}{text}" if text else ""))
        return text
