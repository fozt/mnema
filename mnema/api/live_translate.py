"""Live translation of a phrase by the node's model: one request per phrase, the client
sends the context."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..i18n import _
from ..llm.errors import human_reason
from ..ops import no_model


class PhraseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    context: list[str] = []
    target_lang: str = Field(min_length=1)


def register_live_translate_routes(app: FastAPI) -> None:
    @app.post("/api/translate/phrase")
    def translate_phrase(req: PhraseRequest) -> dict:
        from ..live.translate import live_translate_available, make_live_translator

        if not live_translate_available():
            raise HTTPException(400, no_model())
        translator = make_live_translator(req.target_lang, lambda: req.context)
        try:
            # a callback, as for a live stream: one retry, the phrase does not wait a minute
            text = translator.push(req.text, on_partial=lambda _text: None)
        except Exception as exc:  # noqa: BLE001 - the provider's refusal in the user's words
            raise HTTPException(
                502, _("Translation failed: {reason}", reason=human_reason(exc))
            ) from exc
        return {"text": text}
