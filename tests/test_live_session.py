"""LiveSession orchestration; every collaborator is a structural fake."""

from __future__ import annotations

import pytest

from mnema.core.models import Segment
from mnema.live.models import LiveChunk
from mnema.live.session import LiveSession


class FakeSource:
    def __init__(self, name: str, nblocks: int) -> None:
        self.name = name
        self._blocks = [b"x"] * nblocks
        self.started = False
        self.stopped = False

    def available(self):
        return True

    def start(self):
        self.started = True

    def read(self):
        return self._blocks.pop(0) if self._blocks else None

    def stop(self):
        self.stopped = True


class FakeTranscriber:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def feed(self, pcm): ...
    def poll(self):
        return self._chunks.pop(0) if self._chunks else LiveChunk()

    def finish(self):
        return LiveChunk()


class FakeTranslator:
    def __init__(self):
        self.pushed = []

    def push(self, sentence, on_partial=None):
        self.pushed.append(sentence)
        return f"[{sentence}]"


class FakeView:
    def __init__(self):
        self.committed = []
        self.held = []
        self.lines = []
        self.statuses = []  # separate status channel (warming up, ready, copied)
        self.partials = []
        self.events = []  # shared log: order of partials against commits

    def commit(self, label, text, translation):
        self.committed.append((label, text, translation))
        self.events.append(("commit", label, text, translation))

    def hold(self, label, text):
        self.held.append((label, text))
        self.events.append(("hold", label, text))

    def partial(self, label, text, translation):
        self.partials.append((label, text, translation))
        self.events.append(("partial", label, text, translation))

    def set_line(self, label, text):
        self.lines.append((label, text))

    def set_status(self, label, text):
        self.statuses.append((label, text))


class FakeRecorder:
    def __init__(self):
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _ticking(step: float = 2.0):
    """A clock where each call is step later: any two polls are separated by a speech pause."""
    t = {"v": 0.0}

    def clock() -> float:
        t["v"] += step
        return t["v"]

    return clock


def test_session_accumulates_committed_and_translates_on_sentence() -> None:
    # WLK deltas carry their own leading spaces; the session joins them as is
    chunks = [
        LiveChunk(committed=[Segment(start=0, end=1, text="привет", speaker="mic")], draft="как"),
        LiveChunk(committed=[Segment(start=1, end=2, text=" как дела.", speaker="mic")], draft=""),
    ]
    src = FakeSource("mic", 2)
    tr = FakeTranslator()
    view = FakeView()
    rec = FakeRecorder()
    sess = LiveSession(
        [src], lambda s: FakeTranscriber(chunks), translator=tr, view=view, recorder=rec
    )

    tracks = sess.run()

    assert src.started and src.stopped
    assert rec.started and rec.stopped
    assert len(tracks) == 1
    # the «привет» + «как дела.» deltas become one sentence segment in the Track
    assert [s.text for s in tracks[0].committed] == ["привет как дела."]
    assert tracks[0].committed[0].start == 0 and tracks[0].committed[0].end == 2
    # the sentence ended with a dot on the second chunk → one translation
    assert tr.pushed == ["привет как дела."]
    # the line shows at once (held)…
    assert ("mic", "привет как дела.") in view.held
    # …and is committed together with its translation (run() waits for the queue)
    assert ("mic", "привет как дела.", "[привет как дела.]") in view.committed
    # the current line was shown as the live line meanwhile
    assert any("как" in ln for _, ln in view.lines)


@pytest.mark.parametrize(
    ("deltas", "text"),
    [
        (("провер", "ки."), "проверки."),  # a word in pieces is joined without a space inside
        (("привет", " ."), "привет."),  # punctuation as its own delta, no space before it
    ],
)
def test_session_joins_split_word_without_extra_space(deltas, text) -> None:
    chunks = [
        LiveChunk(committed=[Segment(start=i, end=i + 1, text=d, speaker="mic")])
        for i, d in enumerate(deltas)
    ]
    src = FakeSource("mic", 2)
    tracks = LiveSession([src], lambda s: FakeTranscriber(chunks)).run()
    assert [s.text for s in tracks[0].committed] == [text]


def test_session_dialog_routes_direction_but_labels_by_source() -> None:
    """Dialog mode: route picks the translation direction by the phrase language, but the line is
    always labelled with the source name (mic/system), not the routing «я»/«они»."""
    routed: list[str] = []

    class FakeDialog:
        def route(self, sentence):
            lang = "ru" if any("а" <= c <= "я" for c in sentence.lower()) else "en"
            routed.append(lang)  # direction picked by language
            return "они" if lang == "en" else "я", FakeTranslator()

    chunks = [
        LiveChunk(committed=[Segment(start=0, end=1, text="How long?", speaker="mic")]),
        LiveChunk(committed=[Segment(start=1, end=2, text="Две недели.", speaker="mic")]),
    ]
    src = FakeSource("mic", 2)
    view = FakeView()
    tracks = LiveSession(
        [src], lambda s: FakeTranscriber(chunks), dialog=FakeDialog(), view=view, clock=_ticking()
    ).run()

    assert routed == ["en", "ru"]  # the direction is still picked per phrase
    assert [s.speaker for s in tracks[0].committed] == ["mic", "mic"]  # label = source
    assert ("mic", "How long?", "[How long?]") in view.committed
    assert ("mic", "Две недели.", "[Две недели.]") in view.committed


def test_session_dialog_mapping_routes_by_source() -> None:
    """A "source → Dialog" mapping pins the label and direction to the source (mic/system); the
    phrase language does not affect routing."""

    class PinnedFake:
        def __init__(self, label: str) -> None:
            self.label = label

        def route(self, sentence):
            return self.label, FakeTranslator()

    per_chunks = {
        "mic": [LiveChunk(committed=[Segment(start=0, end=1, text="Sure, done.", speaker="mic")])],
        "system": [
            LiveChunk(committed=[Segment(start=0, end=1, text="Спасибо, ждём.", speaker="system")])
        ],
    }
    mic, system = FakeSource("mic", 1), FakeSource("system", 1)
    trs = {name: FakeTranscriber(chunks) for name, chunks in per_chunks.items()}
    view = FakeView()
    tracks = LiveSession(
        [mic, system],
        lambda s: trs[s.name],
        dialog={"mic": PinnedFake("я"), "system": PinnedFake("они")},
        view=view,
        clock=_ticking(),
    ).run()

    by_label = {t.label: t for t in tracks}
    # the label is always the source name, whatever the language and routing labels
    assert [s.speaker for s in by_label["mic"].committed] == ["mic"]
    assert [s.speaker for s in by_label["system"].committed] == ["system"]
    assert ("mic", "Sure, done.", "[Sure, done.]") in view.committed
    assert ("system", "Спасибо, ждём.", "[Спасибо, ждём.]") in view.committed


def test_session_flushes_incomplete_tail_on_finish() -> None:
    # a sentence without final punctuation is forced to the translator on finish
    chunks = [LiveChunk(committed=[Segment(start=0, end=1, text="без точки", speaker="mic")])]
    src = FakeSource("mic", 1)
    tr = FakeTranslator()
    sess = LiveSession([src], lambda s: FakeTranscriber(chunks), translator=tr)
    sess.run()
    assert tr.pushed == ["без точки"]


def test_session_hides_noise_and_skips_translation_of_fillers() -> None:
    """Non-verbal noise ("Mm-hmm.") is hidden from the view and translation but stays in the
    transcript; a verbal acknowledgement ("Yeah, okay.") shows but is not translated."""
    chunks = [
        LiveChunk(committed=[Segment(start=0, end=1, text="Mm-hmm.", speaker="mic")]),
        LiveChunk(committed=[Segment(start=1, end=2, text=" Yeah, okay.", speaker="mic")]),
        LiveChunk(committed=[Segment(start=2, end=3, text=" Deploy is done.", speaker="mic")]),
    ]
    src = FakeSource("mic", 3)
    tr = FakeTranslator()
    view = FakeView()
    tracks = LiveSession(
        [src], lambda s: FakeTranscriber(chunks), translator=tr, view=view, clock=_ticking()
    ).run()

    # the transcript keeps the raw text
    assert [s.text for s in tracks[0].committed] == ["Mm-hmm.", "Yeah, okay.", "Deploy is done."]
    assert tr.pushed == ["Deploy is done."]  # noise and interjections never reached the translator
    assert not any("Mm-hmm" in text for _, text, _ in view.committed)  # noise hidden
    assert ("mic", "Yeah, okay.", None) in view.committed  # shown, untranslated
    assert ("mic", "Deploy is done.", "[Deploy is done.]") in view.committed  # pair


def test_session_coalesces_short_sentences_without_pause() -> None:
    """A dot alone does not flush: without a pause short sentences accumulate into one line
    ("Sprint 10." / "40." are fragments the translator could not follow)."""
    chunks = [
        LiveChunk(committed=[Segment(start=0, end=1, text="Спринт 10.", speaker="mic")]),
        LiveChunk(committed=[Segment(start=1, end=2, text=" 40.", speaker="mic")]),
        LiveChunk(committed=[Segment(start=2, end=3, text=" Начинаем.", speaker="mic")]),
    ]
    src = FakeSource("mic", 3)
    tracks = LiveSession([src], lambda s: FakeTranscriber(chunks), clock=lambda: 0.0).run()
    assert [s.text for s in tracks[0].committed] == ["Спринт 10. 40. Начинаем."]


def test_session_pause_splits_utterances() -> None:
    """A speech pause (no new deltas) ends the line; the next one goes separately."""
    chunks = [
        LiveChunk(committed=[Segment(start=0, end=1, text="Первая.", speaker="mic")]),
        LiveChunk(committed=[Segment(start=1, end=2, text=" Вторая.", speaker="mic")]),
    ]
    src = FakeSource("mic", 2)
    tracks = LiveSession([src], lambda s: FakeTranscriber(chunks), clock=_ticking()).run()
    assert [s.text for s in tracks[0].committed] == ["Первая.", "Вторая."]


def test_session_flushes_long_sentence_immediately() -> None:
    """A long enough finished line does not wait for a pause."""
    long = "Это достаточно длинное предложение, которое не нужно ни с чем склеивать."
    chunks = [
        LiveChunk(committed=[Segment(start=0, end=1, text=long, speaker="mic")]),
        LiveChunk(committed=[Segment(start=1, end=2, text=" Ещё.", speaker="mic")]),
    ]
    src = FakeSource("mic", 2)
    tracks = LiveSession([src], lambda s: FakeTranscriber(chunks), clock=lambda: 0.0).run()
    assert [s.text for s in tracks[0].committed] == [long, "Ещё."]


class DroppySource(FakeSource):
    """A source reporting losses: drops[i] is dropped_seconds before the i-th read."""

    def __init__(self, name: str, nblocks: int, drops: list[float]) -> None:
        super().__init__(name, nblocks)
        self._drops = list(drops)
        self.dropped_seconds = 0.0

    def read(self):
        if self._drops:
            self.dropped_seconds = self._drops.pop(0)
        return super().read()


def _drop_markers(items) -> list:
    return [x for x in items if "пропущено" in str(x)]


def test_session_announces_drop_episode_once_and_splits_utterance() -> None:
    """A drop episode closes the pending line (speech before and after the gap is not joined) and
    inserts ONE gap marker into the track and the view, not one per frame."""
    chunks = [
        LiveChunk(committed=[Segment(start=0, end=1, text="до разрыва", speaker="mic")]),
        LiveChunk(),  # losses grow: the episode goes on, no marker yet
        LiveChunk(),  # losses stable + lull → announced
        LiveChunk(committed=[Segment(start=9, end=10, text="после разрыва.", speaker="mic")]),
    ]
    src = DroppySource("mic", 4, drops=[0.0, 5.0, 5.0, 5.0])
    view = FakeView()
    tracks = LiveSession(
        [src], lambda s: FakeTranscriber(chunks), view=view, clock=_ticking()
    ).run()

    texts = [s.text for s in tracks[0].committed]
    assert texts[0] == "до разрыва"  # force flush before the marker: the line is closed separately
    assert _drop_markers(texts) == ["[пропущено ~5 с — транскрибация не поспевала]"]
    assert texts.index(_drop_markers(texts)[0]) == 1  # the marker between the pieces
    assert "после разрыва." in texts[-1]
    assert len(_drop_markers([t for _, t, _ in view.committed])) == 1  # and exactly one in the view


@pytest.mark.parametrize(
    ("drop", "markers"),
    [
        (3.0, ["[пропущено ~3 с — транскрибация не поспевала]"]),
        (0.4, []),  # under a second: a marker would be noise
    ],
)
def test_session_announces_drop_tail_on_finish(drop, markers) -> None:
    """Stopped in the middle of a drop episode → the unannounced losses are marked at the end."""
    chunks = [LiveChunk(committed=[Segment(start=0, end=1, text="речь.", speaker="mic")])]
    src = DroppySource("mic", 1, drops=[drop])
    tracks = LiveSession([src], lambda s: FakeTranscriber(chunks), clock=lambda: 0.0).run()

    assert _drop_markers([s.text for s in tracks[0].committed]) == markers


def test_session_streams_partial_translation_into_view() -> None:
    """A streaming translator grows the translation of the held line: view.partial gets the growing
    pieces before the final pair (commit), and the pair itself stays exact."""

    class StreamingTranslator:
        def push(self, sentence, on_partial=None):
            assert on_partial is not None  # with a view the session must pass the callback
            on_partial("Hi")
            on_partial("Hi there")
            return "Hi there."

    chunks = [LiveChunk(committed=[Segment(start=0, end=1, text="привет всем.", speaker="mic")])]
    src = FakeSource("mic", 1)
    view = FakeView()
    LiveSession(
        [src], lambda s: FakeTranscriber(chunks), translator=StreamingTranslator(), view=view
    ).run()

    assert view.partials == [
        ("mic", "привет всем.", "Hi"),
        ("mic", "привет всем.", "Hi there"),
    ]
    # the final pair came after all partials, with the same line text
    last_partial = view.events.index(("partial", "mic", "привет всем.", "Hi there"))
    final_commit = view.events.index(("commit", "mic", "привет всем.", "Hi there."))
    assert last_partial < final_commit


def test_session_commits_pair_even_if_translation_fails() -> None:
    """A translator error does not leave the line held forever: it is committed untranslated."""

    class BoomTranslator:
        def push(self, sentence, on_partial=None):
            raise RuntimeError("api down")

    chunks = [LiveChunk(committed=[Segment(start=0, end=1, text="привет всем.", speaker="mic")])]
    src = FakeSource("mic", 1)
    view = FakeView()
    LiveSession(
        [src], lambda s: FakeTranscriber(chunks), translator=BoomTranslator(), view=view
    ).run()
    assert ("mic", "привет всем.", None) in view.committed


class EnergySource(FakeSource):
    """A source with an energy timeline: "speech" (RMS 2000) on the given intervals, silence (RMS
    50) elsewhere; beyond the horizon None, like a real trimmed window."""

    def __init__(self, name, nblocks, speech=(), horizon=10_000.0) -> None:
        super().__init__(name, nblocks)
        self._speech = list(speech)
        self._horizon = horizon

    def speech_energy(self, start, end):
        if start >= self._horizon:
            return None
        return 2000.0 if any(s < end and e > start for s, e in self._speech) else 50.0


def test_session_drops_hallucination_in_silence() -> None:
    """A segment whose interval falls on actual silence is dropped entirely: not in the Track, not
    shown, not translated; the drop counter grows."""
    chunks = [
        LiveChunk(committed=[Segment(start=1, end=3, text="Реальная фраза.", speaker="mic")]),
        LiveChunk(
            committed=[Segment(start=3, end=20, text=" Да все отлично, а ты как?", speaker="mic")]
        ),
    ]
    src = EnergySource("mic", 2, speech=[(0.0, 3.0)])  # speech only in the first 3 seconds
    tr = FakeTranslator()
    view = FakeView()
    sess = LiveSession(
        [src], lambda s: FakeTranscriber(chunks), translator=tr, view=view, clock=_ticking()
    )
    tracks = sess.run()

    assert [s.text for s in tracks[0].committed] == ["Реальная фраза."]
    assert tr.pushed == ["Реальная фраза."]
    assert not any("отлично" in t for _, t, _ in view.committed)
    assert sess.silence_dropped == {"mic": 1}


@pytest.mark.parametrize(
    ("start", "end", "speech", "horizon"),
    [
        # edge tolerance: ASR timestamps drift, speech ~1 s after the end keeps the segment
        (5, 10, [(11.0, 12.0)], 10_000.0),
        # a short real delta survives the left-edge cut (at most half the interval)
        (10.0, 10.4, [(10.0, 10.4)], 10_000.0),
        # the timeline does not cover the interval → the filter abstains
        (99_998, 99_999, [], 10.0),
    ],
)
def test_session_keeps_segment_with_speech_near_end(start, end, speech, horizon) -> None:
    chunks = [LiveChunk(committed=[Segment(start=start, end=end, text="Реплика.", speaker="mic")])]
    src = EnergySource("mic", 1, speech=speech, horizon=horizon)
    tracks = LiveSession([src], lambda s: FakeTranscriber(chunks)).run()
    assert [s.text for s in tracks[0].committed] == ["Реплика."]


def test_session_leading_bleed_does_not_save_hallucination() -> None:
    """A delta starts where the previous emission ended: the tail of a real phrase at the left
    edge does not save a hallucination, since the left edge is cut by the tolerance."""
    chunks = [LiveChunk(committed=[Segment(start=5, end=15, text="Выдумка.", speaker="mic")])]
    src = EnergySource("mic", 1, speech=[(4.5, 5.4)])  # only the tail of the previous phrase
    sess = LiveSession([src], lambda s: FakeTranscriber(chunks))
    tracks = sess.run()
    assert tracks[0].committed == []
    assert sess.silence_dropped == {"mic": 1}


def test_session_without_speech_energy_keeps_everything() -> None:
    """A source without speech_energy → the filter is off."""
    chunks = [LiveChunk(committed=[Segment(start=3, end=20, text="Без хроники.", speaker="mic")])]
    src = FakeSource("mic", 1)
    sess = LiveSession([src], lambda s: FakeTranscriber(chunks))
    tracks = sess.run()
    assert [s.text for s in tracks[0].committed] == ["Без хроники."]
    assert sess.silence_dropped == {}


def test_session_shifts_gate_by_prebuffer_offset() -> None:
    """The warmup prebuffer dropped old PCM → ASR times are shifted: the session queries the
    timeline offset by the transcriber's fed_offset_seconds."""

    class OffsetTranscriber(FakeTranscriber):
        fed_offset_seconds = 100.0

    chunks = [LiveChunk(committed=[Segment(start=3, end=9, text="После сдвига.", speaker="mic")])]
    src = EnergySource("mic", 1, speech=[(104.0, 108.0)])  # speech in timeline coordinates
    tracks = LiveSession([src], lambda s: OffsetTranscriber(chunks)).run()
    assert [s.text for s in tracks[0].committed] == ["После сдвига."]

    # without the offset the same segment would land on silence and be dropped
    src2 = EnergySource("mic", 1, speech=[(104.0, 108.0)])
    chunks2 = [LiveChunk(committed=[Segment(start=3, end=9, text="После сдвига.", speaker="mic")])]
    tracks2 = LiveSession([src2], lambda s: FakeTranscriber(chunks2)).run()
    assert tracks2[0].committed == []


class TeeSource(FakeSource):
    """A source with a tee (like MicSource): every frame read also goes to the recording."""

    def __init__(self, name: str, blocks: list[bytes], tee) -> None:
        super().__init__(name, 0)
        self._blocks = list(blocks)
        self._tee = tee

    def read(self):
        block = super().read()
        if block:
            self._tee(block)
        return block


def test_session_survives_feed_failure_and_keeps_wav_recording(tmp_path) -> None:
    """ASR raises from feed → recording only: the session runs to the end, the WAV track is whole
    (every frame), the cause is in asr_failures and the marker shows in the view."""
    import wave

    from mnema.live.record import WavTrackWriter

    class BoomFeed:
        def feed(self, pcm):
            raise RuntimeError("движок умер")

        def poll(self):
            return LiveChunk()

        def finish(self):
            return LiveChunk()

    writer = WavTrackWriter(tmp_path / "s.mic.wav")
    src = TeeSource("mic", [b"\x01\x02" * 800] * 3, writer.write)
    view = FakeView()
    sess = LiveSession([src], lambda s: BoomFeed(), view=view, recorder=writer)

    tracks = sess.run()  # does not raise

    assert "движок умер" in sess.asr_failures["mic"]
    assert any("транскрибация недоступна" in t for _, t, _ in view.committed)
    assert tracks[0].committed == []  # nothing was recognized, but the session survived
    with wave.open(str(tmp_path / "s.mic.wav"), "rb") as r:
        assert r.getnframes() == 800 * 3  # every frame, including those read after the failure


def test_session_survives_warmup_failure_as_dictaphone() -> None:
    """The warmup failed (start_warmup raised) → ASR is not fed, but recording lasts the session."""

    class BoomWarmup:
        def start_warmup(self):
            raise RuntimeError("нет модели")

        def feed(self, pcm):
            raise AssertionError("мёртвый ASR не должен кормиться")

        def poll(self):
            return LiveChunk()

        def finish(self):
            return LiveChunk()

    rec = FakeRecorder()
    src = FakeSource("mic", 2)
    view = FakeView()
    sess = LiveSession([src], lambda s: BoomWarmup(), view=view, recorder=[rec])

    tracks = sess.run()

    assert "нет модели" in sess.asr_failures["mic"]
    assert rec.started and rec.stopped  # recording started and closed properly
    assert any("транскрибация недоступна" in t for _, t, _ in view.committed)
    assert tracks[0].committed == []


def test_session_marks_interrupted_transcript_in_track() -> None:
    """The pipeline died mid-speech: the recognized part is in the track, a marker at the break."""

    class FlakyTranscriber:
        def __init__(self):
            self.calls = 0

        def feed(self, pcm):
            self.calls += 1
            if self.calls >= 2:
                raise RuntimeError("пайплайн умер")

        def poll(self):
            if self.calls == 1:
                return LiveChunk(committed=[Segment(start=0, end=1, text="привет.", speaker="mic")])
            return LiveChunk()

        def finish(self):
            return LiveChunk()

    src = FakeSource("mic", 3)
    view = FakeView()
    tracks = LiveSession([src], lambda s: FlakyTranscriber(), view=view, clock=_ticking()).run()

    texts = [s.text for s in tracks[0].committed]
    assert texts[0] == "привет."
    assert "транскрибация недоступна" in texts[1] and "пайплайн умер" in texts[1]


def test_session_survives_finish_failure() -> None:
    """A failure in the final drain still returns the tracks."""

    class BoomFinish:
        def feed(self, pcm): ...

        def poll(self):
            return LiveChunk()

        def finish(self):
            raise RuntimeError("дренаж упал")

    src = FakeSource("mic", 1)
    sess = LiveSession([src], lambda s: BoomFinish())
    tracks = sess.run()
    assert len(tracks) == 1 and "дренаж упал" in sess.asr_failures["mic"]


def test_session_shows_warming_status_until_ready() -> None:
    """While ASR warms up (ready=False) the status says so; once ready an explicit "ready", and
    transcription goes on as usual."""

    class WarmingTranscriber:
        def __init__(self):
            self.feeds = 0

        @property
        def ready(self):
            return self.feeds >= 2

        def feed(self, pcm):
            self.feeds += 1

        def poll(self):
            if self.feeds == 3:
                return LiveChunk(
                    committed=[Segment(start=0, end=1, text="после прогрева.", speaker="mic")]
                )
            return LiveChunk()

        def finish(self):
            return LiveChunk()

    src = FakeSource("mic", 3)
    view = FakeView()
    tracks = LiveSession([src], lambda s: WarmingTranscriber(), view=view).run()

    warm = [i for i, (_, t) in enumerate(view.statuses) if "греется" in t]
    assert len(warm) >= 2  # the warmup status updates (growing seconds counter)
    assert any("готово" in t for _, t in view.statuses[warm[-1] + 1 :])  # then "ready"
    # statuses are not in the live line: speech drafts (set_line, empty too) do not overwrite them
    assert not any("греется" in t or "готово" in t for _, t in view.lines)
    assert [s.text for s in tracks[0].committed] == ["после прогрева."]


def test_session_shows_ready_when_engine_warm_from_start() -> None:
    """The engine is warm from the first block (ready=True at once): "ready" still shows, as the
    user needs the confirmation."""

    class WarmTranscriber:
        ready = True

        def feed(self, pcm): ...

        def poll(self):
            return LiveChunk()

        def finish(self):
            return LiveChunk()

    src = FakeSource("mic", 2)
    view = FakeView()
    LiveSession([src], lambda s: WarmTranscriber(), view=view).run()

    ready_lines = [t for _, t in view.statuses if "готово" in t]
    assert len(ready_lines) == 1  # shown exactly once, not per block
    assert not any("греется" in t for _, t in view.statuses)


def test_session_copy_now_flushes_and_copies_text() -> None:
    """Ctrl-T (copy_now) mid-speech: the buffer is force-flushed and the whole session text goes to
    on_copy; the outcome shows as a note in the view."""
    copied: list[str] = []

    def on_copy(text: str) -> bool:
        copied.append(text)
        return True

    class CopyOnLastSource(FakeSource):
        session: LiveSession | None = None

        def read(self):
            block = super().read()
            if block is not None and not self._blocks:  # the last block was read → "Ctrl-T pressed"
                assert type(self).session is not None
                type(self).session.copy_now()
            return block

    chunks = [
        LiveChunk(committed=[Segment(start=0, end=1, text="раз два", speaker="mic")]),
        LiveChunk(committed=[Segment(start=1, end=2, text=" три", speaker="mic")]),  # no dot
    ]
    src = CopyOnLastSource("mic", 2)
    view = FakeView()
    session = LiveSession([src], lambda s: FakeTranscriber(chunks), view=view, on_copy=on_copy)
    CopyOnLastSource.session = session

    session.run()

    # copied: the force flush brought the unpunctuated tail «три» into the track
    assert copied == ["раз два три"]
    assert any("буфер обмена" in t for _, t in view.statuses)  # the success note is shown


def test_session_copy_now_without_text_reports_nothing_to_copy() -> None:
    """Ctrl-T before the first recognized phrase: a "nothing to copy" note, on_copy not called."""
    copied: list[str] = []
    src = FakeSource("mic", 1)
    view = FakeView()
    session = LiveSession(
        [src],
        lambda s: FakeTranscriber([]),
        view=view,
        on_copy=lambda t: copied.append(t) or True,
    )
    session.copy_now()
    session.run()
    assert copied == []
    assert any("нечего копировать" in t for _, t in view.statuses)


def test_session_announces_notice_change_as_new_marker() -> None:
    """A source notice (microphone reopened) goes to the track and the view once while the value
    stays the same; a changed value (second reopen) is a new marker."""

    class NoticeSource(FakeSource):
        def __init__(self, name, nblocks):
            super().__init__(name, nblocks)
            self.notice = "[микрофон переоткрыт]"

        def read(self):
            if len(self._blocks) == 1:  # by the last block a second reopen happened
                self.notice = "[микрофон переоткрыт ×2]"
            return super().read()

    src = NoticeSource("mic", 3)
    view = FakeView()
    tracks = LiveSession([src], lambda s: FakeTranscriber([]), view=view).run()

    markers = ["[микрофон переоткрыт]", "[микрофон переоткрыт ×2]"]
    assert [s.text for s in tracks[0].committed] == markers
    assert [c for c in view.committed if "переоткрыт" in c[1]] == [
        ("mic", m, None) for m in markers
    ]


def test_session_force_flushes_incomplete_after_long_pause() -> None:
    """An unfinished piece without final punctuation after a LONG pause goes to translation instead
    of waiting for _MAX_PENDING (the short pause flush needs a dot and the size limit is far)."""
    chunks = [
        LiveChunk(
            committed=[Segment(start=0, end=1, text="двадцать слов без точки", speaker="mic")]
        )
    ]
    src = FakeSource("mic", 10)  # silent blocks after speech (empty polls grow idle)
    tr = FakeTranslator()
    view = FakeView()
    LiveSession(
        [src], lambda s: FakeTranscriber(chunks), translator=tr, view=view, clock=_ticking(step=2.0)
    ).run()
    assert (
        tr.pushed and "двадцать слов" in tr.pushed[0]
    )  # force flush on the long pause → translated


def test_session_force_flushes_when_pending_grows_without_punctuation() -> None:
    """Decoding without final punctuation (auto language, continuous speech) does not accumulate
    forever: the line is split by size so translation never starves."""
    words = [
        LiveChunk(committed=[Segment(start=i, end=i + 1, text=f" слово{i}", speaker="mic")])
        for i in range(60)  # ~450 characters without a dot
    ]
    src = FakeSource("mic", len(words))
    tr = FakeTranslator()
    view = FakeView()
    LiveSession(
        [src], lambda s: FakeTranscriber(words), translator=tr, view=view, clock=lambda: 0.0
    ).run()
    assert tr.pushed, "перевод не должен голодать без пунктуации"
    assert len(tr.pushed[0]) <= 400  # split by size, not one giant at the end


def test_session_auto_stops_after_silence_when_speech_stopped() -> None:
    """Speech, then silence ≥ the timeout → the session ends by itself before the blocks run out."""
    # one committed phrase, then silence (empty polls)
    chunks = [LiveChunk(committed=[Segment(start=0, end=1, text="Была речь.", speaker="mic")])]
    src = FakeSource("mic", 50)  # far more blocks than needed before the auto-stop
    view = FakeView()
    sess = LiveSession(
        [src],
        lambda s: FakeTranscriber(chunks),
        view=view,
        clock=_ticking(step=2.0),
        stop_after_silence=5.0,
    )
    tracks = sess.run()

    assert any(
        "авто-стоп" in t for _, t in view.statuses
    )  # the auto-stop fired (status in the view)
    assert src._blocks  # stopped before the source ran out
    assert [s.text for s in tracks[0].committed] == ["Была речь."]  # speech kept


@pytest.mark.parametrize(
    ("texts", "blocks", "step", "stop_after"),
    [
        # while speech goes on (committed deltas every poll) the auto-stop stays quiet
        ([f"Фраза {i}." for i in range(5)], 5, 2.0, 5.0),
        # no speech at all (recording only): time passes, the recording is not cut
        ([], 4, 1000.0, 5.0),
        # stop_after_silence=0 turns the auto-stop off even after long silence
        (["Одна фраза."], 6, 1000.0, 0.0),
    ],
)
def test_session_no_auto_stop_while_speech_flows(texts, blocks, step, stop_after) -> None:
    chunks = [
        LiveChunk(committed=[Segment(start=i, end=i + 1, text=t, speaker="mic")])
        for i, t in enumerate(texts)
    ]
    src = FakeSource("mic", blocks)
    view = FakeView()
    tracks = LiveSession(
        [src],
        lambda s: FakeTranscriber(chunks),
        view=view,
        clock=_ticking(step=step),
        stop_after_silence=stop_after,
    ).run()

    assert not any("авто-стоп" in t for _, t in view.statuses)
    assert src._blocks == []  # reached the end of the source, not cut by the timeout
    assert [s.text for s in tracks[0].committed] == texts


def test_stall_watchdog_announces_frozen_asr() -> None:
    """Stall watchdog: the draft lives but nothing is committed past the limit → a "stalled" status
    (WLK stops committing on a long call and the screen freezes silently)."""

    n = 5
    chunks = [LiveChunk(committed=[], draft=f"растущий черновик {i}") for i in range(n)]
    src = FakeSource("mic", n)
    view = FakeView()
    # each tick is +61 s: by the fifth draft without commits the limit is long passed
    sess = LiveSession([src], lambda s: FakeTranscriber(chunks), view=view, clock=_ticking(61.0))

    sess.run()

    statuses = [txt for _, txt in view.statuses]
    assert any("застопорился" in t for t in statuses)  # an honest status instead of silence
    assert any("rec" in t for t in statuses)  # points to the reliable path without streaming


def test_stall_watchdog_quiet_while_commits_flow() -> None:
    """Commits keep coming → the watchdog stays quiet."""
    chunks = [
        LiveChunk(committed=[Segment(start=i, end=i + 1, text=f"фраза {i}.", speaker="mic")])
        for i in range(4)
    ]
    src = FakeSource("mic", 4)
    view = FakeView()
    sess = LiveSession([src], lambda s: FakeTranscriber(chunks), view=view, clock=_ticking(61.0))

    sess.run()

    assert not any("застопорился" in t for _, t in view.statuses)


def test_draft_tail_capped_on_screen() -> None:
    """A swollen draft shows as its tail (…tail), not thousands of characters."""
    big = "х" * 2000
    chunks = [LiveChunk(committed=[], draft=big)]
    src = FakeSource("mic", 1)
    view = FakeView()
    sess = LiveSession([src], lambda s: FakeTranscriber(chunks), view=view)

    sess.run()

    drawn = [ln for _, ln in view.lines if ln]
    assert drawn and all(len(ln) < 400 for ln in drawn)  # the tail, not the whole sheet
    assert any(ln.startswith("…") for ln in drawn)


class NullLikeTranscriber(FakeTranscriber):
    """A recording-only transcriber (NullTranscriber contract): consumes no audio."""

    ready = True
    consumes_audio = False


def test_session_dictaphone_ignores_queue_drops() -> None:
    """rec mode (NullTranscriber): an overflowing ASR queue is not a recording loss (the tee writes
    the WAV before the queue), so no gap markers, neither in the loop nor at the end."""
    src = DroppySource("mic", 4, drops=[0.0, 5.0, 5.0, 5.0])
    view = FakeView()
    tracks = LiveSession(
        [src], lambda s: NullLikeTranscriber([]), view=view, clock=_ticking()
    ).run()

    assert _drop_markers([s.text for s in tracks[0].committed]) == []
    assert _drop_markers([t for _, t, _ in view.committed]) == []


def test_session_dead_asr_ignores_queue_drops_on_finish() -> None:
    """ASR died (recording only): the track is closed with the "recording audio only" marker and
    queue drops are not announced at the end; the recording is intact."""

    class DyingTranscriber(FakeTranscriber):
        def feed(self, pcm):
            raise RuntimeError("движок умер")

    src = DroppySource("mic", 3, drops=[0.0, 5.0, 5.0])
    tracks = LiveSession([src], lambda s: DyingTranscriber([]), clock=_ticking()).run()

    # committed is empty (failed before any text); the point: no false drop marker
    assert _drop_markers([s.text for s in tracks[0].committed]) == []


def test_stop_translates_backlog_within_budget_then_drops(monkeypatch) -> None:
    """The stop drains the translation backlog within a time budget: what made it is translated,
    the rest goes untranslated (finalize translates everything). Nothing is lost: every line is
    in the track and the view."""
    import time as _time

    import mnema.live.session as session_mod

    monkeypatch.setattr(session_mod, "_STOP_TRANSLATE_WAIT", 0.5)

    class SlowTranslator:
        def __init__(self) -> None:
            self.pushed: list[str] = []

        def push(self, sentence, on_partial=None):
            self.pushed.append(sentence)
            _time.sleep(0.4)  # "the model is thinking"
            return f"[{sentence}]"

    chunks = [
        LiveChunk(committed=[Segment(start=i, end=i + 1, text=f"Фраза {i}.", speaker="mic")])
        for i in range(6)
    ]
    src = FakeSource("mic", 6)
    view = FakeView()
    tr = SlowTranslator()
    t0 = _time.monotonic()
    tracks = LiveSession(
        [src], lambda s: FakeTranscriber(chunks), translator=tr, view=view, clock=_ticking()
    ).run()
    elapsed = _time.monotonic() - t0

    assert len(tracks[0].committed) == 6  # the track is whole
    committed_texts = [t for _, t, _ in view.committed]
    assert sorted(committed_texts) == sorted(f"Фраза {i}." for i in range(6))  # all in the view
    assert 1 <= len(tr.pushed) < 6  # some made the budget, not the whole backlog
    assert elapsed < 2.0  # not 6 × 0.4 s of sequential translations


def test_partial_startup_closes_opened_sources_and_recorders(tmp_path) -> None:
    import pytest

    from mnema.core.base import MediaError
    from mnema.live.record import WavTrackWriter

    class FailedStart(FakeSource):
        def start(self):
            super().start()
            raise MediaError("device disappeared")

    mic = FakeSource("mic", 0)
    system = FailedStart("system", 0)
    writer = WavTrackWriter(tmp_path / "mic.wav")
    session = LiveSession([mic, system], lambda s: FakeTranscriber([]), recorder=writer)
    with pytest.raises(MediaError, match="device disappeared"):
        session.run()
    assert mic.stopped and system.stopped and writer._wav is None


def test_failed_stop_does_not_skip_other_resources() -> None:
    class FailedStop(FakeSource):
        def stop(self):
            raise OSError("stop failed")

    mic = FakeSource("mic", 0)
    system = FailedStop("system", 0)
    recorder = FakeRecorder()
    session = LiveSession([mic, system], lambda s: FakeTranscriber([]), recorder=recorder)
    session.run()
    assert mic.stopped and recorder.stopped
    assert session.recording_error == "системный звук перестал записываться"


def test_disk_failure_stops_capture_and_preserves_available_text(monkeypatch, tmp_path) -> None:
    import errno
    from unittest.mock import Mock

    from mnema.live.record import WavTrackWriter

    disk = Mock()
    disk.writeframes.side_effect = [None, OSError(errno.ENOSPC, "disk full")]
    monkeypatch.setattr("mnema.live.record.wave.open", lambda *a: disk)
    writer = WavTrackWriter(tmp_path / "mic.wav")
    mic = TeeSource("mic", [b"\x10\x27" * 800] * 3, writer.write)
    transcriber = FakeTranscriber([LiveChunk(committed=[Segment(start=0, end=1, text="успело")])])
    view = FakeView()
    session = LiveSession([mic], lambda s: transcriber, recorder=writer, view=view)
    tracks = session.run()
    assert mic.stopped and len(mic._blocks) == 1
    assert session.recording_error == "микрофон перестал записываться"
    assert any("Запись остановлена" in status for _, status in view.statuses)
    assert [segment.text for segment in tracks[0].committed] == ["успело"]
    disk.close.assert_called_once()


def test_source_tee_failure_is_visible_to_session() -> None:
    from mnema.live.system_audio import _BufferedSource

    class Source(_BufferedSource):
        def start(self):
            self._emit(b"\x10\x27" * 100)

        def stop(self):
            self._stopped = True

    def failed_write(pcm):
        raise OSError("disk full")

    source = Source(label="system", tee=failed_write, block_seconds=1)
    session = LiveSession([source], lambda s: FakeTranscriber([]))
    session.run()
    assert source._stopped and source.recording_error
    assert session.recording_error == "системный звук перестал записываться"


def _cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in text)


def test_session_statuses_and_markers_are_english_in_english_ui() -> None:
    """English UI: progress, warmup, ready, copy note, gap marker and stop statuses have no
    Cyrillic."""
    from mnema.i18n import using

    class WarmingTranscriber:
        def __init__(self):
            self.feeds = 0

        @property
        def ready(self):
            return self.feeds >= 2

        def feed(self, pcm):
            self.feeds += 1

        def poll(self):
            if self.feeds == 4:
                return LiveChunk(committed=[Segment(start=0, end=1, text="Hello.", speaker="mic")])
            return LiveChunk()

        def finish(self):
            return LiveChunk()

    progress: list[str] = []
    src = DroppySource("mic", 6, drops=[0.0, 0.0, 0.0, 5.0, 5.0, 5.0])
    view = FakeView()
    session = LiveSession(
        [src],
        lambda s: WarmingTranscriber(),
        translator=FakeTranslator(),
        view=view,
        clock=_ticking(),
        on=lambda _f, msg: progress.append(msg),
        on_copy=lambda t: True,
    )
    session.copy_now()
    with using("en"):
        tracks = session.run()

    statuses = [t for _, t in view.statuses]
    shown = [*progress, *statuses, *(s.text for s in tracks[0].committed)]
    assert progress == ["Listening… Ctrl-C to stop"]
    assert any(t.startswith("⏳ Recognition engine warming up") for t in statuses)
    assert "✅ Ready — speak" in statuses
    assert "📋 Nothing to copy yet" in statuses
    assert "⏹ Stopping: finishing the transcript…" in statuses
    assert "[skipped ~5 s — transcription couldn't keep up]" in shown
    assert not [t for t in shown if _cyrillic(t)]


def test_session_failure_texts_are_english_in_english_ui() -> None:
    """English UI: the recording-only marker after an ASR failure and the auto-stop status."""
    from mnema.i18n import using

    class BoomFeed(FakeTranscriber):
        def feed(self, pcm):
            if not self._chunks:
                raise RuntimeError("engine crashed")

    chunks = [LiveChunk(committed=[Segment(start=0, end=1, text="Hi there.", speaker="mic")])]
    view = FakeView()
    with using("en"):
        session = LiveSession([FakeSource("mic", 3)], lambda s: BoomFeed(chunks), view=view)
        tracks = session.run()
        auto = FakeView()
        LiveSession(
            [FakeSource("mic", 50)],
            lambda s: FakeTranscriber(chunks),
            view=auto,
            clock=_ticking(30.0),
            stop_after_silence=60.0,
        ).run()

    marker = "[transcription unavailable: engine crashed — recording audio only]"
    assert tracks[0].committed[-1].text == marker
    assert ("mic", marker, None) in view.committed
    assert ("mic", "⏹ Auto-stop: 1 min of silence") in auto.statuses
    assert not [t for _, t in auto.statuses if _cyrillic(t)]
