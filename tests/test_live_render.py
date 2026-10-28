"""Live view: speaker turns, translation pairs and streaming.

rich.Live runs on a StringIO console: the scrollback is observed through printing, the live area
through _renderable(). The turn timeout uses an injected clock.
"""

from __future__ import annotations

import io
import threading

import pytest
from rich.console import Console

from mnema.live.render import (
    LiveView,
    PaneView,
    _Entry,
    _pane_cells,
    _Turn,
    visible_turns,
)


def _view(*sources: str, clock=None, **kw) -> tuple[LiveView, io.StringIO]:
    out = io.StringIO()
    console = Console(file=out, width=2000)  # wide: long paragraphs do not wrap
    if clock is not None:
        kw["clock"] = clock
    return LiveView(list(sources) or ["mic"], console=console, **kw), out


def _plains(v: LiveView) -> list[str]:
    return [r.plain for r in v._renderable().renderables]


def test_liveview_draft_inlines_into_open_turn_not_bottom_row() -> None:
    """The draft tail is drawn dimmed inside the open turn's block, not as a separate row at the
    bottom; before the first block the draft is a row of its own."""
    v, _ = _view("mic")
    v.set_line("mic", "прив")  # speech starts: no block yet, the draft is its own row
    assert _plains(v) == ["mic⋯ прив"]
    v.commit("mic", "Привет.", None)  # the first block appears
    v.set_line("mic", "как дела")  # tail
    assert _plains(v) == ["mic: Привет. как дела"]  # tail in the block, no bottom row


def test_draft_inlines_despite_label_mismatch_dialog_mode() -> None:
    """Dialog mode: the turn is labelled by speaker («они»), the draft by source ("mic"). The only
    draft still attaches to the open block instead of a separate row at the bottom."""
    v, _ = _view("mic")
    v.commit("они", "Масква, Масква!", None)  # the block is labelled «они», the source is mic
    v.set_line("mic", "лиса, включи трек")
    plains = _plains(v)
    assert plains == ["они: Масква, Масква! лиса, включи трек"]  # tail in the block
    assert not any(p.startswith("mic⋯") for p in plains)  # no separate bottom row


def test_status_shows_when_draft_empty_and_survives_empty_set_line() -> None:
    """A status shows while the draft is empty; an empty set_line (sent every tick) keeps it.

    Statuses used to live in the live line, where _emit overwrote them with an empty draft in the
    same tick, so they never reached the screen."""
    v, _ = _view("mic")
    v.set_status("mic", "✅ готово — говорите")
    v.set_line("mic", "")  # a session tick without speech
    assert "✅ готово — говорите" in " ".join(_plains(v))


def test_draft_takes_priority_over_status_and_speech_clears_it() -> None:
    """The speech draft wins over the status; after speech the status does not come back."""
    v, _ = _view("mic")
    v.set_status("mic", "✅ готово — говорите")
    v.set_line("mic", "привет, я")  # speech starts
    plains = " ".join(_plains(v))
    assert "привет, я" in plains and "готово" not in plains
    v.set_line("mic", "")  # the phrase flushed, the draft is empty
    assert "готово" not in " ".join(_plains(v))  # the status is done and gone


def test_status_expires_by_staleness() -> None:
    """A stale status is cleared by the same timeout as drafts."""
    t = {"v": 0.0}
    v, _ = _view("mic", clock=lambda: t["v"])
    v.set_status("mic", "📋 скопировано в буфер обмена")
    assert "скопировано" in " ".join(_plains(v))
    t["v"] = 30.0  # well past _LINE_STALE
    assert "скопировано" not in " ".join(_plains(v))


def test_consecutive_replicas_of_same_speaker_grow_one_block() -> None:
    """Consecutive lines of one speaker are one growing paragraph, not «они: …» rows."""
    v, out = _view("mic")
    with v:
        v.commit("они", "Первая.", None)
        v.commit("они", "Вторая.", None)
        # live area: a row per line (append-only, wraps never change while it grows)
        plains = _plains(v)
        assert "они: Первая." in plains and "  Вторая." in plains
        assert "Первая" not in out.getvalue()  # the turn is open, not in the scrollback yet


def test_turn_flushes_to_history_on_speaker_change() -> None:
    v, out = _view("mic")
    with v:
        v.commit("я", "Готово.", None)
        v.commit("они", "Спасибо.", None)
        assert "я: Готово." in out.getvalue()  # speaker change → the whole turn left
        assert "они: Спасибо." in _plains(v)  # the new turn is in the live area
        assert "Спасибо" not in out.getvalue()


def test_turn_flushes_on_inactivity_timeout_and_same_speaker_starts_new_block() -> None:
    t = {"v": 0.0}
    v, out = _view("mic", clock=lambda: t["v"])
    with v:
        v.commit("они", "Первая.", None)
        t["v"] = 31.0
        v.commit("они", "Вторая.", None)
        assert "они: Первая." in out.getvalue()  # the stale turn left on timeout
        assert "они: Вторая." in _plains(v)  # the continuation is a new block


def test_stale_turn_flushes_on_new_live_line() -> None:
    """New speech after a lull (live line) pushes the stale turn to the scrollback."""
    t = {"v": 0.0}
    v, out = _view("mic", clock=lambda: t["v"])
    with v:
        v.commit("они", "Старая.", None)
        t["v"] = 31.0
        v.set_line("mic", "нов")
        assert "они: Старая." in out.getvalue()


def test_giant_monologue_splits_by_size() -> None:
    """An endless monologue does not grow one block forever: past the size the turn leaves and the
    speaker continues in a new block."""
    v, out = _view("mic")
    long = "х" * 400 + "."
    with v:
        v.commit("они", long, None)
        v.commit("они", long, None)  # over the limit in total → the first turn is closed
        v.commit("они", "Хвост.", None)
        assert long in out.getvalue()
        assert "они: Хвост." in _plains(v)


def test_pairs_attach_in_order_within_turn() -> None:
    """Translations follow the line order and attach to the block when ready."""
    v, _ = _view("mic")
    with v:
        v.hold("я", "Раз.")
        v.commit("я", "Раз.", "One.")
        v.hold("я", "Два.")
        v.commit("я", "Два.", "Two.")
        # live area: a pair under each line; the scrollback gets a joined paragraph
        assert _plains(v)[:4] == ["я: Раз.", "  ⟶  One.", "  Два.", "  ⟶  Two."]


def test_pending_replica_marks_translation_in_flight() -> None:
    v, _ = _view("mic")
    with v:
        v.hold("я", "Привет.")
        plains = _plains(v)
        assert "я: Привет." in plains and "  ⟶  …" in plains


def test_turn_waits_for_translations_before_history_in_order() -> None:
    """A turn stays while its translations are in flight; the scrollback order is chronological
    even if the next turn finished earlier."""
    v, out = _view("mic")
    with v:
        v.hold("я", "Раз.")
        v.hold("я", "Два.")
        v.commit("я", "Раз.", "One.")
        v.commit(
            "они", "Ок.", None
        )  # speaker change: the «я» turn is closed, but «Два.» is in flight
        assert "Раз" not in out.getvalue()  # does not leave without its pair
        v.commit("я", "Два.", "Two.")  # the last translation arrived → the whole turn left
        assert "я: Раз. Два." in out.getvalue() and "One. Two." in out.getvalue()
        assert "Ок" not in out.getvalue()  # the next turn is still live
    hist = out.getvalue()
    assert hist.index("я: Раз. Два.") < hist.index("они: Ок.")  # the order holds on exit too


def test_untranslated_replica_joins_paragraph_without_translation() -> None:
    """A line without a translation ("Yeah, okay.") grows the paragraph but not the translations."""
    v, _ = _view("mic")
    with v:
        v.hold("я", "Раз.")
        v.commit("я", "Раз.", "One.")
        v.commit("я", "Ок.", None)
        plains = _plains(v)
        assert "  Ок." in plains  # the line is in the turn...
        assert not any("Ок" in p and "⟶" in p for p in plains)  # ...without a translation row


@pytest.mark.parametrize("make", [lambda: _view("mic"), lambda: _pane("mic")])
def test_exit_flushes_unpaired_replicas_without_translation(make) -> None:
    """Lines still waiting for a translation are not lost: the turn leaves without it (the
    full-screen view has its own exit that dumps the whole session)."""
    v, out = make()
    with v:
        v.hold("я", "Без пары.")
    assert "я: Без пары." in out.getvalue()
    assert "⟶" not in out.getvalue()


def test_partial_translation_grows_in_pending_block() -> None:
    v, _ = _view("mic")
    with v:
        v.hold("я", "Привет, как дела?")
        v.partial("я", "Привет, как дела?", "Hi, how")
        assert any("⟶  Hi, how…" in p for p in _plains(v))
        v.partial("я", "Привет, как дела?", "Hi, how are")
        assert any("⟶  Hi, how are…" in p for p in _plains(v))
        v.commit("я", "Привет, как дела?", "Hi, how are you?")
        plains = _plains(v)
        assert "я: Привет, как дела?" in plains and "  ⟶  Hi, how are you?" in plains


def test_partial_for_unknown_replica_is_ignored() -> None:
    """A partial without a held line (a race at the boundary) neither fails nor creates blocks."""
    v, _ = _view("mic")
    v.partial("я", "нет такой", "tail")
    assert _plains(v) == []  # no blocks, no placeholders


def test_immediate_commit_clears_live_line() -> None:
    v, _ = _view("mic")
    v.set_line("mic", "текущая")
    v.commit("mic", "готово.", None)
    assert not any("mic⋯" in p for p in _plains(v))  # the live line was cleared


def test_paired_commit_keeps_next_live_line() -> None:
    v, _ = _view("mic")
    v.hold("mic", "готово.")
    v.set_line("mic", "следующая фраза")
    v.commit("mic", "готово.", "done.")
    assert any("следующая фраза" in p for p in _plains(v))  # the paired commit leaves it alone


def _pane(*sources: str, width: int = 2000, **kw) -> tuple[PaneView, io.StringIO]:
    out = io.StringIO()
    console = Console(file=out, width=width)
    return PaneView(list(sources) or ["mic"], console=console, translate=True, **kw), out


def _turn(label: str, *texts: str, translation: str | None = None) -> _Turn:
    return _Turn(label=label, entries=[_Entry(text=t, translation=translation) for t in texts])


def test_pane_cells_pair_originals_and_translations_per_turn() -> None:
    """A row per turn: label and original on the left, translations in line order on the right."""
    turn = _Turn(
        label="они",
        entries=[
            _Entry(text="Раз.", translation="One."),
            _Entry(text="Ок."),  # an untranslated interjection does not break the right column
            _Entry(text="Два.", translation="Two."),
        ],
    )
    left, right = _pane_cells(turn)
    assert left.plain == "они: Раз. Ок. Два."
    assert right.plain == "One. Two."


def test_visible_turns_follow_tail_by_height() -> None:
    """The tail of turns that fits the height is shown (a turn = 1 row plus a gap)."""
    turns = [_turn(f"t{i}", "текст") for i in range(10)]
    assert visible_turns(turns, height=5, col_width=80) == turns[-3:]  # 3 turns + 2 gaps
    assert visible_turns(turns, height=100, col_width=80) == turns  # everything fits


def test_visible_turns_always_keep_last_turn() -> None:
    giant = _turn("они", "х" * 500)
    assert visible_turns([giant], height=3, col_width=20) == [giant]


def test_visible_turns_account_for_wrapping_in_both_columns() -> None:
    """A row is as tall as its taller column: a long original or a long translation takes rows."""
    long_left = _turn("они", "х" * 100)  # «они: » + 100 chars → 6 rows at width 20
    long_right = _turn("я", "ок", translation="о" * 100)  # a 100-char translation → 5 rows
    tail = _turn("я", "ок")
    assert visible_turns([long_left, tail], height=4, col_width=20) == [tail]
    assert visible_turns([long_right, tail], height=4, col_width=20) == [tail]


def test_pane_partial_translation_grows_in_cell() -> None:
    """Full screen rewrites what is shown: a growing translation updates in its cell."""
    v, _ = _pane("mic")
    v.hold("я", "Привет, как дела?")
    assert _pane_cells(v._turns[0])[1].plain == "…"  # translation in flight
    v.partial("я", "Привет, как дела?", "Hi, how")
    assert _pane_cells(v._turns[0])[1].plain == "Hi, how…"
    v.commit("я", "Привет, как дела?", "Hi, how are you?")
    assert _pane_cells(v._turns[0])[1].plain == "Hi, how are you?"


def test_pane_prints_nothing_to_scrollback_during_session() -> None:
    """Finished turns stay on screen instead of being printed to the scrollback."""
    v, out = _pane("mic")
    with v:
        v.commit("я", "Готово.", "Done.")
        v.commit("они", "Спасибо.", "Thanks.")  # speaker change: the first turn is done
        assert out.getvalue() == ""
        assert [t.label for t in [*v._done, *v._turns]] == ["я", "они"]  # both visible


def test_pane_exit_dumps_whole_session_to_console_in_order() -> None:
    """On exit the whole result is printed to the normal terminal at once, in chronological order
    and with translations."""
    v, out = _pane("mic")
    with v:
        v.hold("я", "Раз.")
        v.commit("я", "Раз.", "One.")
        v.commit("они", "Ок.", None)
    hist = out.getvalue()
    assert "я: Раз." in hist and "⟶  One." in hist and "они: Ок." in hist
    assert hist.index("я: Раз.") < hist.index("они: Ок.")


def test_pane_layout_puts_translation_beside_original_and_shows_hint() -> None:
    """The translation is on the same terminal row as the original (next column); the Ctrl-C hint
    is at the bottom."""
    v, out = _pane("mic", width=100)
    v.commit("я", "Привет.", "Hi.")
    v._console.print(v._renderable())
    lines = out.getvalue().splitlines()
    row = next(line for line in lines if "я: Привет." in line)
    assert "Hi." in row
    assert any("Ctrl-C" in line for line in lines)


def test_view_survives_concurrent_mutation() -> None:
    """The session loop (set_line/hold/set_status) and the translation worker (commit/partial) hit
    the view from different threads. Without the RLock a race on _turns breaks _drain (pop(0) on
    empty) or loses a turn; with it, no thread errors and a consistent final state."""
    v, _ = _view("mic", "sys")
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def hammer(worker: int) -> None:
        barrier.wait()  # start together for maximum overlap
        try:
            for i in range(300):
                label = "mic" if (worker + i) % 2 == 0 else "sys"
                text = f"w{worker}-{i}"
                v.set_line(label, text)
                v.hold(label, text)
                v.partial(label, text, "…")
                v.commit(label, text, "tr")
                v.set_status(label, "готово")
        except BaseException as exc:  # noqa: BLE001 - record any race from the thread
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"гонка в потоках: {errors[:3]}"
    # close and drain everything: the turn queue must empty without errors
    with v._lock:
        for turn in v._turns:
            turn.closed = True
            for e in turn.entries:
                e.pending = False
        v._drain()
    assert v._turns == []


def test_pane_exit_dump_holds_lock() -> None:
    """The final dump of _done in PaneView.__exit__ runs under the lock. If the translation worker
    is still alive (its join timed out), its commit→_flush appends to _done and an unlocked dump
    would fail with "list changed size during iteration". Checked directly: while the dump
    prints, another thread cannot take the lock."""

    class _DummyLive:  # was_live=True without a real rich.Live (no alt screen on StringIO)
        def update(self, *a, **k) -> None: ...
        def stop(self) -> None: ...

    lock_was_held: list[bool] = []
    v, _ = _pane("mic")

    class _ProbeConsole(Console):
        def print(self, *a, **k):  # called by the dump from the lock-owning thread
            box: list[bool] = []

            def grab() -> None:
                got = v._lock.acquire(blocking=False)  # from another thread
                box.append(got)
                if got:
                    v._lock.release()

            t = threading.Thread(target=grab)
            t.start()
            t.join()
            lock_was_held.append(not box[0])  # True if the dump held the lock
            return super().print(*a, **k)

    v.commit("mic", "Раз.", "One.")  # open turn
    v.commit("sys", "Два.", "Two.")  # label change → the mic turn closed and went to _done
    v._console = _ProbeConsole(file=io.StringIO(), width=200)
    v._live = _DummyLive()
    v.__exit__(None, None, None)

    assert lock_was_held and all(lock_was_held)  # every dump print ran under the lock


def test_pane_hint_is_english_in_english_ui() -> None:
    from mnema.i18n import using

    v, out = _pane("mic", width=100)
    v.commit("mic", "Hello.", "Привет.")
    with using("en"):
        v._console.print(v._renderable())
    text = out.getvalue()
    assert "Ctrl-T copy text · Ctrl-C stop" in text
    assert "текст в буфер" not in text
