"""Remarks after processing (live/remarks) in the user's words: cause, effect, what to do; the
menu takes short lines from the fields, never by searching the text."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from mnema.i18n import using
from mnema.live import remarks
from mnema.live.models import STEP_MESSAGES, title_message
from mnema.live.remarks import Step
from mnema.llm.errors import ErrorKind
from mnema.llm.notes import Cloud, LightNote

UNTIL = datetime(2026, 10, 3, 9, 0).astimezone()


def _failed(kind: ErrorKind | None, **kw) -> LightNote:
    return LightNote(
        engine="none", cloud=Cloud.FAILED, kind=kind, model="gemini-flash-latest", **kw
    )


def test_menu_lines_one_cause_then_packed_effects() -> None:
    """Menu: the main cause first, then the effects without repeats in lines of up to 58
    characters."""
    cloud = LightNote(engine="none", cloud=Cloud.FAILED, cloud_detail="x")
    items = [
        remarks.light(Step.SUMMARY, cloud),
        remarks.light(Step.TITLE, cloud),
        remarks.mix_failed("ffmpeg exit 1"),
        remarks.translation_failed(),
    ]
    lines = remarks.menu_lines([r for r in items if r is not None])
    assert lines == [
        "Модель не ответила",
        "Без пересказа, без названия, без общего mp3, без перевода",
    ]
    assert all(len(ln) <= remarks.MENU_WIDTH for ln in lines)
    assert remarks.menu_lines([]) == []


def test_command_goes_on_its_own_line_as_is() -> None:
    """A command is copied: on its own line, with no period or quotes around it; the text
    before it ends with a colon, not ":."."""
    r = remarks.Remark(
        step=Step.ASR,
        cause="Дорожка не распознана",
        hint="Обработайте запись ещё раз:",
        command="mnema finish '/Users/me/mnema/2026-09-27_18-33'",
    )
    assert r.text == (
        "Дорожка не распознана. Обработайте запись ещё раз:\n"
        "mnema finish '/Users/me/mnema/2026-09-27_18-33'"
    )
    assert remarks.Remark(step=Step.MIX, cause="Нет mp3").text == "Нет mp3."  # no command


def test_merged_one_cause_for_several_model_steps() -> None:
    """The cloud refused: the summary and the title share one cause, so one remark with the
    cause once, effects in step order and no repeated hints; other causes and other steps
    (recognition) stay separate."""
    summary = remarks.Remark(
        step=Step.SUMMARY, cause="Облако недоступно", effect="Краткого содержания нет",
        hint="Пополните счёт", short="Облако недоступно", short_effect="без краткого содержания",
    )  # fmt: skip
    title = remarks.Remark(
        step=Step.TITLE, cause="Облако недоступно", effect="Папка названа по времени записи",
        hint="Пополните счёт. Название можно задать вручную", short_effect="без названия",
    )  # fmt: skip
    other = remarks.Remark(step=Step.TITLE, cause="Другая причина", short_effect="без названия")
    asr = remarks.Remark(step=Step.ASR, cause="Облако недоступно", short="Дорожка")
    out = remarks.merged([summary, asr, title, other])
    assert [r.cause for r in out] == ["Облако недоступно", "Облако недоступно", "Другая причина"]
    one = out[0]
    assert one.effect == "Краткого содержания нет. Папка названа по времени записи"
    assert one.hint == "Пополните счёт. Название можно задать вручную"
    assert one.short_effect == "без краткого содержания, без названия"
    assert one.text.count("Облако недоступно") == 1 and one.text.count("Пополните счёт") == 1
    assert out[1].step is Step.ASR
    assert summary.effect == "Краткого содержания нет"  # the original remarks are untouched


def test_menu_lines_rank_server_and_asr_above_models_and_cap_at_three() -> None:
    """The main cause in the menu goes by importance, not order: server and recognition above
    the models; never more than three lines however many effects there are."""
    cloud = LightNote(engine="none", cloud=Cloud.FAILED, cloud_detail="x")
    summary = remarks.light(Step.SUMMARY, cloud)
    assert summary is not None
    asr = remarks.asr_failed("mic", "нет движка", live_kept=True)
    assert remarks.menu_lines([summary, asr])[0] == "Дорожка «Микрофон» не распознана"
    server = remarks.server_down("Сервер", "mnema finish '/x'")
    assert remarks.menu_lines([summary, asr, server])[0] == "Сервер недоступен"
    many = [
        remarks.Remark(step=Step.MIX, cause=f"п{i}", short_effect=f"следствие номер {i} " * 2)
        for i in range(8)
    ]
    lines = remarks.menu_lines([summary, *many])
    assert len(lines) == 3 and lines[0] == "Модель не ответила"
    assert all(len(ln) <= remarks.MENU_WIDTH for ln in lines)


def test_until_text_names_day_beyond_24_hours_and_in_saved_remarks() -> None:
    """Limit reset time: within a day just the time, later and in stored remarks with the
    date."""
    now = datetime(2026, 10, 2, 14, 5).astimezone()
    soon = now.replace(hour=9, minute=0) + timedelta(days=1)
    assert remarks.until_text(soon, now) == "до 09:00"
    assert remarks.until_text(soon, now, dated=True) == "до 3 окт. 09:00"
    assert remarks.until_text(soon + timedelta(days=1), now) == "до 4 окт. 09:00"


def test_limit_steps_minutes_apart_are_still_one_remark() -> None:
    """The title and the summary hit the limit a minute apart: the reset times in the causes
    differ, yet it is one remark."""
    title = remarks.light(Step.TITLE, _failed(ErrorKind.RATE_LIMIT, until=UNTIL))
    summary = remarks.light(
        Step.SUMMARY, _failed(ErrorKind.RATE_LIMIT, until=UNTIL + timedelta(minutes=1))
    )
    assert title is not None and summary is not None and title.cause != summary.cause
    [one] = remarks.merged([title, summary])
    assert one.effect == "Запись названа по времени. Пересказа нет"


_CYRILLIC = re.compile("[А-Яа-яЁё]")


def _remarks_in_english() -> list[remarks.Remark]:
    limit = _failed(ErrorKind.RATE_LIMIT, until=UNTIL)
    found = [
        remarks.light(Step.TITLE, limit),
        remarks.light(Step.SUMMARY, limit),
        remarks.light(Step.SUMMARY, LightNote(engine="none", cloud=Cloud.NOT_SET)),
        remarks.light(Step.SUMMARY, _failed(ErrorKind.INVALID_KEY)),
        remarks.light(Step.TITLE, _failed(None, cloud_detail="HTTP 502: Bad Gateway")),
        remarks.recording_failed("disk full"),
        remarks.asr_failed("mic", "no engine", live_kept=True),
        remarks.asr_failed("system", "no engine", live_kept=False),
        remarks.track_silent("system", "blocked"),
        remarks.track_silent("system", "none"),
        remarks.track_silent("mic"),
        remarks.asr_no_speech("system", live_kept=False),
        remarks.node_no_model(),
        remarks.summary_failed("runner error"),
        remarks.summary_garbled(),
        remarks.title_failed("timeout"),
        remarks.translation_failed(),
        remarks.mix_failed("ffmpeg exit 1"),
        remarks.server_down("Home"),
        remarks.server_down("Home", "mnema finish '/x'"),
    ]
    return [r for r in found if r is not None]


def test_remarks_menu_and_details_render_in_english() -> None:
    with using("en"):
        items = _remarks_in_english()
        texts = [r.text for r in remarks.merged(items)]
        menu = remarks.menu_lines(items)
        details = remarks.details(texts, ["The full recording was moved to the Trash"])
        stopped = [remarks.track_stopped(label) for label in ("mic", "system", "Screen")]
        fallback = remarks.menu_lines([remarks.Remark(step=Step.MIX, cause="x")])
        steps = list(STEP_MESSAGES.values()) + [title_message()]
        not_saved = remarks.files_not_saved()
    rendered = [*texts, *menu, details, *stopped, *fallback, *steps, not_saved]
    assert not [t for t in rendered if _CYRILLIC.search(t)]
    assert menu[0] == "Home is unavailable"
    assert details.startswith("Notes\n\n- ") and "\n\nAbout this recording\n\n- " in details
    assert stopped == [
        "the microphone stopped recording",
        "system audio stopped recording",
        "screen stopped recording",
    ]
    assert fallback == ["With notes"]


def test_english_remarks_read_as_sentences() -> None:
    with using("en"):
        silent = remarks.track_silent("system", "blocked")
        key = remarks.light(Step.SUMMARY, _failed(ErrorKind.INVALID_KEY))
        down = remarks.server_down("Home")
        summary = remarks.summary_failed("HTTP 503 from the runner")
    assert (
        silent.text
        == "The “System audio” track has no sound: the phone did not provide the call audio."
    )
    assert key is not None and key.text == (
        "Model gemini-flash-latest did not respond: the model key was rejected. There is no "
        "summary. Replace the model key on the server. When the model works again, choose "
        "“Summarize” in the recording menu."
    )
    assert (
        down.text
        == "Home is unavailable. The audio is saved. Choose “Retry” on the recording card."
    )
    assert summary.cause == "The summary was not written: the server is restarting or unavailable"


def test_plain_error_keeps_the_runner_option() -> None:
    assert (
        remarks.plain_error("Нет движка — задай --runner URL") == "Нет движка — задай --runner URL"
    )
    assert remarks.plain_error("раннер не ответил") == "сервер не ответил"
    assert remarks.plain_error("the runner did not answer") == "the server did not answer"
