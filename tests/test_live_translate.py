"""Live translator units; claude._stream_text is mocked at the boundary (no network)."""

from __future__ import annotations

import httpx
import pytest

from mnema.live.translate import DeepLTranslator, LiveTranslator
from mnema.llm import claude


def test_live_translate_available(monkeypatch) -> None:
    """Live translation is available with DeepL or a configured model, otherwise not."""
    from mnema.live.translate import live_translate_available

    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    monkeypatch.delenv("MNEMA_MODEL", raising=False)
    assert live_translate_available() is False
    monkeypatch.setenv("MNEMA_MODEL", "anthropic/claude-haiku-4-5")
    assert live_translate_available() is True  # model only
    monkeypatch.delenv("MNEMA_MODEL", raising=False)
    monkeypatch.setenv("DEEPL_API_KEY", "dk")
    assert live_translate_available() is True  # DeepL only


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


@pytest.mark.parametrize(
    "make", [lambda: LiveTranslator("ru"), lambda: DeepLTranslator("en", api_key="k:fx")]
)
def test_push_empty_returns_empty(monkeypatch, make) -> None:
    import mnema.live.translate as tr_mod

    monkeypatch.setattr(claude, "_stream_text", lambda *a, **k: "should-not-call")
    monkeypatch.setattr(tr_mod.httpx, "post", _fake_httpx_post([], reply="should-not-call"))
    assert make().push("   ") == ""


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


def _fake_httpx_post(captured: list, reply: str = "translated"):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"translations": [{"text": reply}]}

    def post(url, *, json=None, headers=None, timeout=None):
        captured.append({"url": url, "json": json, "headers": headers})
        return Resp()

    return post


@pytest.mark.parametrize(
    ("key", "host", "lang", "target"),
    [
        ("k:fx", "https://api-free.deepl.com/", "en", "EN-US"),  # ":fx" is the Free plan
        ("k", "https://api.deepl.com/", "ru", "RU"),
    ],
)
def test_deepl_free_key_routes_to_free_host_and_maps_lang(monkeypatch, key, host, lang, target):
    import mnema.live.translate as tr_mod

    captured: list = []
    monkeypatch.setattr(tr_mod.httpx, "post", _fake_httpx_post(captured))

    out = DeepLTranslator(lang, api_key=key).push("Привет.")
    assert out == "translated"
    assert captured[0]["url"].startswith(host)
    assert captured[0]["json"]["target_lang"] == target
    assert captured[0]["headers"]["Authorization"] == f"DeepL-Auth-Key {key}"


def test_deepl_context_carries_previous_pairs(monkeypatch) -> None:
    """The second phrase goes with the first one's "line ⟶ translation" as context."""
    import mnema.live.translate as tr_mod
    from mnema.live.translate import DeepLTranslator

    captured: list = []
    monkeypatch.setattr(tr_mod.httpx, "post", _fake_httpx_post(captured, reply="one"))
    d = DeepLTranslator("en", api_key="k:fx")
    d.push("Первая.")
    d.push("Вторая.")
    assert "Первая. ⟶ one" in captured[1]["json"]["context"]


def test_make_live_translator_prefers_deepl_when_key_set(monkeypatch) -> None:
    """DeepL and a model → fallback; DeepL alone → plain DeepL; no DeepL → LiveTranslator."""
    from mnema.live.translate import (
        DeepLTranslator,
        LiveTranslator,
        _FallbackTranslator,
        make_live_translator,
    )

    monkeypatch.setenv("DEEPL_API_KEY", "k:fx")  # conftest sets a model → fallback
    assert isinstance(make_live_translator("en"), _FallbackTranslator)
    monkeypatch.delenv("MNEMA_MODEL", raising=False)  # no model → plain DeepL
    assert isinstance(make_live_translator("en"), DeepLTranslator)
    monkeypatch.delenv("DEEPL_API_KEY")
    assert isinstance(make_live_translator("en"), LiveTranslator)


def _status_error(status: int, url: str = "https://api-free.deepl.com/v2/translate"):
    req = httpx.Request("POST", url)
    return httpx.HTTPStatusError("err", request=req, response=httpx.Response(status, request=req))


def test_deepl_retries_once_on_transient(monkeypatch) -> None:
    """429 (overload or rate limit) → one retry → success, the phrase is not lost."""
    import mnema.live.translate as tr_mod

    monkeypatch.setattr(tr_mod.time, "sleep", lambda s: None)
    n = {"calls": 0}

    class Resp:
        def raise_for_status(self):
            n["calls"] += 1
            if n["calls"] == 1:
                raise _status_error(429)

        def json(self):
            return {"translations": [{"text": "ok"}]}

    monkeypatch.setattr(tr_mod.httpx, "post", lambda *a, **k: Resp())
    assert tr_mod.DeepLTranslator("en", api_key="k:fx").push("Привет.") == "ok"
    assert n["calls"] == 2  # the first (429) and the retry


def test_deepl_no_retry_on_client_error(monkeypatch) -> None:
    """400/456 (bad request or quota): a retry will not help, it raises at once."""
    import pytest

    import mnema.live.translate as tr_mod

    n = {"calls": 0}

    class Resp:
        def raise_for_status(self):
            n["calls"] += 1
            raise _status_error(456)  # quota exceeded is not transient

        def json(self):
            return {"translations": [{"text": "x"}]}

    monkeypatch.setattr(tr_mod.httpx, "post", lambda *a, **k: Resp())
    with pytest.raises(httpx.HTTPStatusError):
        tr_mod.DeepLTranslator("en", api_key="k:fx").push("Привет.")
    assert n["calls"] == 1  # no retry


def test_fallback_translator_uses_llm_when_deepl_fails() -> None:
    """DeepL failed even after a retry → the LLM fallback translates the phrase."""
    from mnema.live.translate import _FallbackTranslator

    class BadPrimary:
        def push(self, s, on_partial=None):
            raise RuntimeError("deepl down")

    class Stub:
        def push(self, s, on_partial=None):
            return "llm:" + s

    ft = _FallbackTranslator(BadPrimary(), lambda: Stub())
    assert ft.push("привет") == "llm:привет"
