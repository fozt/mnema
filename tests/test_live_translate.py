"""Live translator units; claude._stream_text is mocked at the boundary (no network)."""

from __future__ import annotations

from mnema.live.translate import LiveTranslator
from mnema.llm import claude


def test_live_translate_available(monkeypatch) -> None:
    """Live translation is available with a configured model, otherwise not."""
    from mnema.live.translate import live_translate_available

    monkeypatch.delenv("MNEMA_MODEL", raising=False)
    assert live_translate_available() is False
    monkeypatch.setenv("MNEMA_MODEL", "anthropic/claude-haiku-4-5")
    assert live_translate_available() is True


def test_push_uses_rolling_context_window(monkeypatch) -> None:
    """The context holds "line ⟶ translation" pairs of the window's last lines."""
    seen: list[tuple[str, str]] = []

    def fake_stream(system, content, **kw):
        seen.append((system, content))
        return f"<{content}>"

    monkeypatch.setattr(claude, "_stream_text", fake_stream)
    tr = LiveTranslator("ru", context=2)
    tr.push("A.")
    tr.push("B.")
    tr.push("C.")

    last_system, last_content = seen[-1]
    assert last_content == "C."  # only the current line is translated
    assert "B. ⟶ <B.>" in last_system  # the previous one is in the context with its translation
    assert "A." not in last_system  # fell out of the window (context=2)


def test_push_empty_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(claude, "_stream_text", lambda *a, **k: "should-not-call")
    assert LiveTranslator("ru").push("   ") == ""


def test_push_streams_growing_translation_via_on_partial(monkeypatch) -> None:
    """push with on_partial passes the growing translation while it is generated."""

    def fake_stream(system, content, *, on_text=None, **kw):
        if on_text is not None:
            on_text("Прив")
            on_text("Привет")
        return "Привет"

    monkeypatch.setattr(claude, "_stream_text", fake_stream)
    got: list[str] = []
    assert LiveTranslator("en").push("Hello.", on_partial=got.append) == "Привет"
    assert got == ["Прив", "Привет"]


def test_worth_translating_filters_fillers_and_nonletters() -> None:
    """Interjections and lines without letters are not translated; normal speech is."""
    from mnema.live.translate import worth_translating

    for junk in (
        "Mm-hmm.",
        "Uh...",
        "Okay. Okay.",
        "Yeah, yep.",
        "Угу.",
        "Э-э...",
        "...",
        "19 0 33",
    ):
        assert not worth_translating(junk), junk
    for speech in ("Okay, I will try it.", "Думаю, около двух недель.", "P... on him right now."):
        assert worth_translating(speech), speech


def test_strip_noise_removes_nonlexical_junk() -> None:
    """Non-verbal noise (uh, um, oh, э-э…) is removed; words containing those letters are not."""
    from mnema.live.translate import strip_noise

    assert strip_noise("Uh... in one hour. I hope.") == "in one hour. I hope."
    assert strip_noise("Oh. Back three.") == "Back three."
    assert strip_noise("Э-э... сорок сорок два.") == "сорок сорок два."
    assert strip_noise("I think, uh, that it works") == "I think, that it works"
    assert strip_noise("Uh... Mm.") == ""  # pure noise → empty (the line is hidden)
    # word boundaries: Ohio/думаю/эх stay
    assert strip_noise("Ohio is ahead") == "Ohio is ahead"
    assert strip_noise("я думаю, эх, ладно") == "я думаю, эх, ладно"


def test_strip_noise_collapses_dot_runs() -> None:
    """Runs of dots (whisper on pauses) → one "…"; single dots and "e.g." stay."""
    from mnema.live.translate import strip_noise

    assert strip_noise("So...... I will... to participation") == "So… I will… to participation"
    assert strip_noise("Wait. . . done") == "Wait… done"
    assert strip_noise("Hello. World.") == "Hello. World."  # sentence dots stay
    assert strip_noise("e.g. the case") == "e.g. the case"  # letters between the dots: stays


def test_dialog_routes_by_language(monkeypatch) -> None:
    """A phrase in my language → «я» and translated into theirs; in theirs → «они» and into mine."""
    from mnema.live.translate import LiveDialog

    captured: list[tuple[str, str]] = []

    def fake_stream(system, content, **kw):
        captured.append((system, content))
        return f"<{content}>"

    monkeypatch.setattr(claude, "_stream_text", fake_stream)
    d = LiveDialog("ru")  # my language ru → theirs en (automatic)
    assert d.their_lang == "en"

    label, tr = d.route("Думаю, около двух недель.")
    assert label == "я"
    tr.push("Думаю, около двух недель.")
    assert "en" in captured[-1][0] or "англ" in captured[-1][0].lower()  # into their language

    label, tr = d.route("How long will it take?")
    assert label == "они"
    tr.push("How long will it take?")
    assert "ru" in captured[-1][0] or "рус" in captured[-1][0].lower()  # into my language


def test_pinned_dialog_fixes_label_and_direction(monkeypatch) -> None:
    """The label is pinned by source, the direction follows the phrase language: on an English
    call my English phrase is translated for me into Russian."""
    from mnema.live.translate import LiveDialog, PinnedDialog

    captured: list[tuple[str, str]] = []

    def fake_stream(system, content, **kw):
        captured.append((system, content))
        return f"<{content}>"

    monkeypatch.setattr(claude, "_stream_text", fake_stream)
    d = LiveDialog("ru")
    mic, system = PinnedDialog(d, "я"), PinnedDialog(d, "они")

    label, tr = mic.route("Ok, deploying now.")  # English call: my phrase in English
    assert label == "я"  # label by source
    tr.push("Ok, deploying now.")
    assert (
        "ru" in captured[-1][0] or "рус" in captured[-1][0].lower()
    )  # translated for me into Russian

    label, tr = system.route("Секунду, проверяю.")  # the other side speaks my language
    assert label == "они"
    tr.push("Секунду, проверяю.")
    assert "en" in captured[-1][0] or "англ" in captured[-1][0].lower()  # direction by language


def test_pinned_dialog_shares_context_with_parent(monkeypatch) -> None:
    """Pinned routes write into the shared LiveDialog window: the translator sees both sides."""
    from mnema.live.translate import LiveDialog, PinnedDialog

    captured: list[tuple[str, str]] = []

    def fake_stream(system, content, **kw):
        captured.append((system, content))
        return f"<{content}>"

    monkeypatch.setattr(claude, "_stream_text", fake_stream)
    d = LiveDialog("ru")
    PinnedDialog(d, "они").route("Any updates on the deploy?")
    _, tr = PinnedDialog(d, "я").route("Всё выкатили.")
    tr.push("Всё выкатили.")

    assert "они: Any updates on the deploy?" in captured[-1][0]


def test_dialog_context_is_shared_across_directions(monkeypatch) -> None:
    """Both directions see the shared window of labelled lines, so the translator reads the talk
    as a dialog (their question is visible while my answer is translated)."""
    from mnema.live.translate import LiveDialog

    captured: list[tuple[str, str]] = []

    def fake_stream(system, content, **kw):
        captured.append((system, content))
        return f"<{content}>"

    monkeypatch.setattr(claude, "_stream_text", fake_stream)
    d = LiveDialog("ru")
    _, theirs = d.route("How long will it take?")
    theirs.push("How long will it take?")
    _, tr = d.route("Думаю, две недели.")
    tr.push("Думаю, две недели.")

    system = captured[-1][0]
    # the other direction's line is in the context with its translation
    assert "они: How long will it take? ⟶ <How long will it take?>" in system
