"""Language of the summary and the folder title: the bar setting reaches the model request;
without one, the language of the conversation.

The boundary is the offline pass (setup.dispatch) and the cloud model (litellm), as in
test_live_setup.
"""

from __future__ import annotations

import json

import pytest
from test_live_setup import (
    _cloud_model,
    _fake_dispatch,
    _live_tracks,
    _patch_build,
    _write_wav,
    build_live,
)

import mnema.live.setup as setup_mod
from mnema.live.setup import LiveConfig, finalize_live

SPOKEN = {
    "ru": "Обсудили сроки релиза и кто что берёт. " * 20,
    "en": "We agreed on the release date and who takes what. " * 20,
}
NAMES = {"ru": "русский", "en": "английский"}


@pytest.mark.parametrize(
    ("spoken", "summary_lang", "asked"),
    [
        ("ru", "en", "en"),
        ("en", "ru", "ru"),
        ("ru", None, "ru"),
        ("en", None, "en"),
        ("ru", "auto", "ru"),
    ],
)
def test_summary_request_names_chosen_language_else_conversation_language(
    monkeypatch, tmp_path, spoken, summary_lang, asked
) -> None:
    """ru/en in the settings: that language goes to the model (the folder title comes from the same
    pass); unset or not ru/en: the language of the conversation."""
    _patch_build(monkeypatch, {})
    text = SPOKEN[spoken]
    segments = [{"start": 0.0, "end": 60.0, "text": text}]
    _fake_dispatch(monkeypatch, {"text": text, "segments": segments})
    systems: list[str] = []

    def completion(system, content):
        systems.append(system)
        answer = {"title": "Созвон", "about": "О релизе.", "summary": ["Пункт."]}
        return json.dumps(answer, ensure_ascii=False)

    _cloud_model(monkeypatch, completion)
    config = LiveConfig(summary_lang=summary_lang)
    prepared = build_live(config, base=tmp_path / "live-s")
    _write_wav(prepared)
    finalize_live(prepared, _live_tracks(), retitle=False)

    [system] = systems
    assert [code for code, name in NAMES.items() if f"строго {name}" in system] == [asked]


@pytest.mark.parametrize(
    ("spoken", "summary", "summary_lang", "asked"),
    [
        ("short", True, "en", "en"),
        ("short", True, None, "ru"),
        # The summary failed: no separate title request after the same refusal.
        ("long", True, "en", None),
        ("long", False, "en", "ru"),
    ],
)
def test_fallback_title_uses_chosen_language(
    monkeypatch, tmp_path, spoken, summary, summary_lang, asked
) -> None:
    """The fallback title of a short recording (no summary) is in the chosen language too; unset,
    or summaries off (the choice is then unavailable): the language of the conversation. When the
    model gave no summary, no separate title is requested (same refusal)."""
    _patch_build(monkeypatch, {})
    text = SPOKEN["ru"] if spoken == "long" else "Привет, это проверка связи."
    _fake_dispatch(
        monkeypatch, {"text": text, "segments": [{"start": 0.0, "end": 60.0, "text": text}]}
    )
    systems: list[str] = []

    def cloud_down(system, content, **k):
        systems.append(system)
        raise RuntimeError("DeepseekException - Insufficient Balance")

    monkeypatch.setattr(setup_mod.claude, "_stream_text", cloud_down)
    config = LiveConfig(summary=summary, summary_lang=summary_lang)
    prepared = build_live(config, base=tmp_path / "live-t")
    _write_wav(prepared)
    finalize_live(prepared, _live_tracks())

    title = [code for s in systems for code, name in NAMES.items() if f"языке: {name}" in s]
    assert title == ([asked] if asked else [])
