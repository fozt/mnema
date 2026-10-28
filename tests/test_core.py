"""Core unit tests: pure logic without ffmpeg, models or network."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mnema.core import base
from mnema.core.backends import _SegmentParser, local_snapshot_dir
from mnema.core.models import Segment, Transcript
from mnema.core.probe import probe
from mnema.i18n import using

# ─────────────────────────────────────────── formatting


@pytest.mark.parametrize(
    ("ts", "expected"),
    [("00:05.000", 5.0), ("01:30", 90.0), ("1:00:00", 3600.0)],
)
def test_ts_to_sec(ts: str, expected: float) -> None:
    assert base.ts_to_sec(ts) == pytest.approx(expected)


def _cyrillic(text: str) -> bool:
    return any("\u0400" <= ch <= "\u04ff" for ch in text)


def test_core_errors_read_in_english(tmp_path: Path) -> None:
    with using("en"):
        with pytest.raises(base.MediaError) as missing:
            base.run(["__mnema_no_such_binary__", "-version"])
        with pytest.raises(base.MediaError) as failed:
            base.run([sys.executable, "-c", "import sys; sys.exit(2)"])
        with pytest.raises(base.MediaError) as no_file:
            probe(tmp_path / "missing.mp4")
    assert (
        str(missing.value)
        == "__mnema_no_such_binary__ not found. Install ffmpeg or reinstall Mnema"
    )
    assert "exit code 2" in str(failed.value)
    assert str(no_file.value).startswith("File not found: ")
    assert not any(_cyrillic(str(e.value)) for e in (missing, failed, no_file))


@pytest.mark.parametrize(
    ("size", "ru", "en"),
    [(512, "512 Б", "512 B"), (1536, "1.5 КБ", "1.5 KB"), (5 * 1024**2, "5.0 МБ", "5.0 MB"),
     (3 * 1024**3, "3.0 ГБ", "3.0 GB"), (5000 * 1024**3, "5000.0 ГБ", "5000.0 GB")],
)  # fmt: skip
def test_fmt_size_follows_the_interface_language(size: int, ru: str, en: str) -> None:
    assert base.fmt_size(size) == ru
    with using("en"):
        assert base.fmt_size(size) == en


def test_run_gives_the_child_no_stdin() -> None:
    """Without stdin closed ffmpeg reads stdin: the process input (terminal keys, the app's
    messages to the bar engine) never reaches the child."""
    script = (
        "import sys\n"
        "from mnema.core.base import run\n"
        "print(run([sys.executable, '-c', 'import sys; print(len(sys.stdin.read()))']))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], input="сообщение\n", capture_output=True, text=True
    )
    assert proc.stdout.strip() == "0", proc.stderr


def test_run_reads_any_bytes_from_ffmpeg() -> None:
    """ffmpeg prints Cyrillic paths and tags in foreign encodings; Mnema.app without a locale read
    them as ASCII. Output is read as UTF-8 and stray bytes do not break it."""
    child = (
        "import sys; sys.stderr.buffer.write('Рабочий созвон'.encode() + b' \\xff'); sys.exit(1)"
    )
    with pytest.raises(base.MediaError, match="Рабочий созвон"):
        base.run([sys.executable, "-c", child])


# ─────────────────────────────────────────── codec choice by platform


@pytest.mark.parametrize(
    ("system", "encoders", "hwaccel", "expected"),
    [
        (
            "Darwin",
            {"hevc_videotoolbox"},
            ["-hwaccel", "videotoolbox"],
            {"hevc_videotoolbox", "-prio_speed"},
        ),
        ("Linux", {"hevc_nvenc", "libx265"}, ["-hwaccel", "cuda"], {"hevc_nvenc"}),
        ("Linux", {"libx265"}, [], {"libx265"}),  # no hardware codecs: CPU
    ],
)
def test_hevc_profile_mac_uses_videotoolbox(
    monkeypatch, system, encoders, hwaccel, expected
) -> None:
    from mnema.core import video

    monkeypatch.setattr(video.platform, "system", lambda: system)
    monkeypatch.setattr(video, "_ffmpeg_encoders", lambda: frozenset(encoders))
    got_hwaccel, codec = video._hevc_profile(60)
    assert got_hwaccel == hwaccel
    assert expected <= set(codec)


def test_concat_list_escapes_apostrophe_in_path() -> None:
    """merge(): an apostrophe in a name is escaped, or the ffmpeg concat demuxer fails."""
    from pathlib import Path

    from mnema.core.video import _concat_list

    out = _concat_list([Path("/tmp/Alice's talk.mp4"), Path("/tmp/plain.mp4")])
    # resolve() canonicalizes the path (/tmp → /private/tmp on macOS), so check substrings.
    assert "Alice'\\''s talk.mp4'" in out  # ' escaped as '\''
    assert "Alice's talk" not in out  # no raw apostrophe left inside quotes
    assert "plain.mp4'" in out  # a plain path is intact


def _merge_env(monkeypatch, infos):
    """merge() without ffmpeg: probe returns infos in a loop; both run (stream copy) and
    _ffmpeg_with_progress (re-encoding) record commands in a shared list."""
    from mnema.core import video

    it = iter(infos + infos)  # merge probes the inputs, then the result
    cmds: list[list[str]] = []
    monkeypatch.setattr(video, "probe", lambda p: next(it))
    monkeypatch.setattr(video, "run", lambda cmd, **kw: cmds.append(cmd))
    monkeypatch.setattr(video, "_ffmpeg_with_progress", lambda cmd, *, total, on: cmds.append(cmd))
    return cmds


_FULL_FFMPEG = frozenset({"libmp3lame", "aac", "libopus", "libvorbis", "opus", "vorbis"})
# the Mac app's LGPL build
_LGPL_FFMPEG = frozenset({"libmp3lame", "aac", "opus", "vorbis"})


@pytest.mark.parametrize(
    ("suffix", "encoders", "codec"),
    [
        (".mp3", _FULL_FFMPEG, ["libmp3lame"]),
        (".m4a", _FULL_FFMPEG, ["aac"]),
        (".webm", _FULL_FFMPEG, ["libopus"]),
        (".webm", _LGPL_FFMPEG, ["opus", "-strict", "experimental"]),
        (".ogg", _LGPL_FFMPEG, ["vorbis", "-strict", "experimental"]),
    ],
)
def test_merge_audio_only_mixed_codecs_reencodes_without_video_args(
    monkeypatch, tmp_path, suffix, encoders, codec
) -> None:
    """concat mp3+aac (different codecs): re-encoding WITHOUT video options, the audio codec
    follows the output container (mp3 takes no aac, .webm takes no aac); without libopus or
    libvorbis the native encoder does."""
    from mnema.core import video
    from mnema.core.probe import MediaInfo

    infos = [
        MediaInfo(path=Path("a.mp3"), duration=1, size=1, audio_codec="mp3", video_codec=None),
        MediaInfo(path=Path("b.aac"), duration=1, size=1, audio_codec="aac", video_codec=None),
    ]
    cmds = _merge_env(monkeypatch, infos)
    monkeypatch.setattr(video, "_ffmpeg_encoders", lambda: encoders)

    video.merge([Path("a.mp3"), Path("b.aac")], tmp_path / f"joined{suffix}")

    assert len(cmds) == 1  # no stream copy attempt (codecs differ): straight to re-encoding
    cmd = cmds[0]
    # audio is merged with the concat FILTER (the demuxer drops pieces on mixed codecs)
    assert "-filter_complex" in cmd and "-tag:v" not in cmd
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=2:v=0:a=1" in fc and "aresample" in fc  # inputs normalized
    assert cmd.count("-i") == 2  # both inputs passed directly, not as a demuxer list
    at = cmd.index("-c:a") + 1
    assert cmd[at : at + len(codec)] == codec


def test_merge_mixed_video_keeps_hevc_reencode(monkeypatch, tmp_path) -> None:
    """Merging videos with mismatched parameters re-encodes to HEVC."""
    from mnema.core import video
    from mnema.core.probe import MediaInfo

    infos = [
        MediaInfo(
            path=Path("a.mov"),
            duration=1,
            size=1,
            audio_codec="aac",
            video_codec="h264",
            width=1920,
        ),
        MediaInfo(
            path=Path("b.mov"),
            duration=1,
            size=1,
            audio_codec="aac",
            video_codec="hevc",
            width=1280,
        ),
    ]
    cmds = _merge_env(monkeypatch, infos)
    monkeypatch.setattr(video, "_ffmpeg_encoders", lambda: frozenset({"libx265"}))

    video.merge([Path("a.mov"), Path("b.mov")], tmp_path / "joined.mov")

    cmd = cmds[0]
    # video is merged with the concat FILTER too: every input is scaled and padded to the first
    # one's canvas, then HEVC
    assert "-filter_complex" in cmd and "-tag:v" in cmd
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=2:v=1:a=1" in fc and "scale=1920:" in fc and "pad=1920:" in fc
    assert cmd.count("-i") == 2  # both inputs passed directly, not as a demuxer list
    assert cmd[cmd.index("-c:a") + 1] == "aac"


# ─────────────────────────────────────────── transcript format


def test_timed_paragraph_uses_first_nonempty_segment() -> None:
    tr = Transcript(
        text="Привет. Как дела? Норм.",
        segments=[
            Segment(start=0, end=1, text=" ", speaker="mic"),
            Segment(start=12.6, end=14, text="Привет.", speaker="mic"),
            Segment(start=14.5, end=16, text="Как дела?", speaker="mic"),
            Segment(start=20, end=22, text="Норм.", speaker="system"),
        ],
    )
    assert tr.with_speakers(timed=True) == ("[0:12] mic: Привет. Как дела?\n\n[0:20] system: Норм.")
    assert tr.with_speakers() == "mic: Привет. Как дела?\n\nsystem: Норм."


TRANSCRIPTS = json.loads(
    (Path(__file__).parent / "fixtures/transcript-v1.json").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", TRANSCRIPTS, ids=lambda case: case["name"])
def test_transcript_file_is_the_shared_one_the_phone_reads(case) -> None:
    """transcript.txt of the shared fixture, which the phone's card also parses (parseDialog)."""
    segments = [Segment(**s) for s in case["segments"]]
    tr = Transcript(
        text=" ".join(s.text for s in segments), segments=segments, language=case["language"]
    )
    assert tr.with_speakers(timed=True) == case["text"]


def test_timed_unlabelled_paragraphs_and_text_without_segments() -> None:
    tr = Transcript(
        text="Один. Два.",
        segments=[
            Segment(start=12.6, end=14, text="Один."),
            Segment(start=3723, end=3724, text="Два."),
        ],
    )
    assert tr.with_speakers(timed=True) == "[0:12] Один.\n\n[1:02:03] Два."
    assert Transcript(text="Без сегментов").with_speakers(timed=True) == "Без сегментов"


@pytest.mark.parametrize(("gap", "separator"), [(1.0, "\n\nsystem: ")])
def test_with_speakers_breaks_same_track_at_pause(gap: float, separator: str) -> None:
    tr = Transcript(
        text="Первую задачу не закончили. Следующая задача — API.",
        segments=[
            Segment(start=0, end=2, text="Первую задачу не закончили.", speaker="system"),
            Segment(start=2 + gap, end=6, text="Следующая задача — API.", speaker="system"),
        ],
    )
    assert tr.with_speakers() == (
        f"system: Первую задачу не закончили.{separator}Следующая задача — API."
    )


def test_with_speakers_ignores_empty_segments_without_losing_pause() -> None:
    tr = Transcript(
        text="Один. Два.",
        segments=[
            Segment(start=0, end=1, text="Один.", speaker="system"),
            Segment(start=1, end=3, text="  ", speaker="mic"),
            Segment(start=3, end=4, text="Два.", speaker="system"),
        ],
    )
    assert tr.with_speakers() == "system: Один.\n\nsystem: Два."


def test_overlapping_segments_do_not_create_a_false_pause() -> None:
    tr = Transcript(
        text="Длинная реплика. Вставка. Продолжение.",
        segments=[
            Segment(start=0, end=10, text="Длинная реплика.", speaker="system"),
            Segment(start=2, end=3, text="Вставка.", speaker="system"),
            Segment(start=5, end=11, text="Продолжение.", speaker="system"),
        ],
    )
    assert tr.with_speakers() == "system: Длинная реплика. Вставка. Продолжение."


def test_unlabelled_transcript_keeps_paragraphs_in_both_formats() -> None:
    tr = Transcript(
        text="Один. Раз. Два.",
        segments=[
            Segment(start=0, end=1, text="Один."),
            Segment(start=1.2, end=1.8, text="Раз."),  # under a second: the same paragraph
            Segment(start=3, end=4, text="Два."),
        ],
    )
    assert tr.with_speakers() == tr.as_text() == "Один. Раз.\n\nДва."


def test_detect_lang() -> None:
    from mnema.llm import claude

    assert claude._detect_lang("hello world, this is plain english") == "en"
    assert claude._detect_lang("привет, это русский текст про созвон") == "ru"
    # more English text → en despite a couple of Russian words
    assert claude._detect_lang("mostly english text here with пара слов") == "en"


# ─────────────────────────────────────────── mlx verbose output parser


def test_segment_parser_extracts_segments_and_language() -> None:
    captured: list = []
    parser = _SegmentParser(total=10.0, on=lambda frac, msg: captured.append((frac, msg)))
    parser.write("Detected language: Russian\n")
    parser.write("[00:00.000 --> 00:05.000] привет\n")
    parser.write("[00:05.000 --> 00:10.000] пока\n")
    assert parser.language == "Russian"
    assert [s.text for s in parser.segments] == ["привет", "пока"]
    assert parser.segments[1].end == pytest.approx(10.0)
    # progress is end/total
    assert captured[-1] == (pytest.approx(1.0), "Распознаю…")


def test_segment_parser_progress_reads_in_english() -> None:
    """live/setup matches this exact message to name the track being recognized."""
    captured: list[str] = []
    parser = _SegmentParser(total=10.0, on=lambda frac, msg: captured.append(msg))
    with using("en"):
        parser.write("[00:00.000 --> 00:05.000] hello\n")
    assert captured == ["Recognizing…"]


def test_segment_parser_buffers_partial_lines() -> None:
    parser = _SegmentParser(total=0.0, on=lambda f, m: None)
    parser.write("[00:00.000 --> 00:01.0")
    assert parser.segments == []  # the line is not finished yet
    parser.write("00] кусок\n")
    assert [s.text for s in parser.segments] == ["кусок"]


# ─────────────────────────────────────────── model lookup in the cache (no network)


def test_model_cache_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    repo = "mlx-community/whisper-test"
    assert local_snapshot_dir(repo) is None

    snap = tmp_path / "hub" / "models--mlx-community--whisper-test" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "weights.safetensors").write_bytes(b"x" * 10)
    (tmp_path / "hub" / "models--mlx-community--whisper-test" / "refs").mkdir(parents=True)
    (tmp_path / "hub" / "models--mlx-community--whisper-test" / "refs" / "main").write_text("abc")

    assert local_snapshot_dir(repo) == snap


def test_merge_raises_on_silent_material_loss(monkeypatch, tmp_path) -> None:
    """Completeness guard: ffmpeg "succeeded" but the result is much shorter than the parts →
    a loud error, not a silently incomplete file."""
    from mnema.core import video
    from mnema.core.base import MediaError
    from mnema.core.probe import MediaInfo

    src_infos = [
        MediaInfo(path=Path("a.mp3"), duration=100, size=1, audio_codec="mp3", video_codec=None),
        MediaInfo(path=Path("b.aac"), duration=100, size=1, audio_codec="aac", video_codec=None),
    ]
    short_result = MediaInfo(
        path=Path("joined.m4a"), duration=100, size=1, audio_codec="aac", video_codec=None
    )  # 100 of 200: half is lost
    seq = iter(src_infos + [short_result])
    monkeypatch.setattr(video, "probe", lambda p: next(seq))
    monkeypatch.setattr(video, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(video, "_ffmpeg_with_progress", lambda cmd, *, total, on: None)

    with pytest.raises(MediaError, match="потеряла материал"):
        video.merge([Path("a.mp3"), Path("b.aac")], tmp_path / "joined.m4a")


def test_merge_video_to_webm_uses_vp9(monkeypatch, tmp_path) -> None:
    """WebM takes no HEVC: a re-encoding video merge into .webm encodes VP9."""
    from mnema.core import video
    from mnema.core.probe import MediaInfo

    infos = [
        MediaInfo(
            path=Path("a.webm"),
            duration=1,
            size=1,
            audio_codec="opus",
            video_codec="vp9",
            width=320,
            height=240,
        ),
        MediaInfo(
            path=Path("b.webm"),
            duration=1,
            size=1,
            audio_codec="opus",
            video_codec="vp9",
            width=640,
            height=360,
        ),
    ]
    cmds = _merge_env(monkeypatch, infos)

    video.merge([Path("a.webm"), Path("b.webm")], tmp_path / "joined.webm")

    cmd = cmds[0]
    assert "libvpx-vp9" in cmd and "-tag:v" not in cmd  # VP9, not HEVC/hvc1


def test_merge_video_into_audio_container_dies(monkeypatch, tmp_path) -> None:
    """Video parts into an audio container (.m4a) give a clear error, not a cryptic ffmpeg one."""
    from mnema.core import video
    from mnema.core.base import MediaError
    from mnema.core.probe import MediaInfo

    infos = [
        MediaInfo(
            path=Path("a.mp4"),
            duration=1,
            size=1,
            audio_codec="aac",
            video_codec="h264",
            width=320,
            height=240,
        ),
        MediaInfo(
            path=Path("b.mov"),
            duration=1,
            size=1,
            audio_codec="aac",
            video_codec="h264",
            width=640,
            height=360,
        ),
    ]
    _merge_env(monkeypatch, infos)

    with pytest.raises(MediaError, match="аудио-контейнер"):
        video.merge([Path("a.mp4"), Path("b.mov")], tmp_path / "joined.m4a")


def test_merge_mixed_audio_and_video_inputs_die(monkeypatch, tmp_path) -> None:
    """An audio file among video parts has no [i:v]: a clear error instead of a filter crash."""
    from mnema.core import video
    from mnema.core.base import MediaError
    from mnema.core.probe import MediaInfo

    infos = [
        MediaInfo(
            path=Path("a.mp4"),
            duration=1,
            size=1,
            audio_codec="aac",
            video_codec="h264",
            width=320,
            height=240,
        ),
        MediaInfo(path=Path("b.mp3"), duration=1, size=1, audio_codec="mp3", video_codec=None),
    ]
    _merge_env(monkeypatch, infos)

    with pytest.raises(MediaError, match="нет видеопотока"):
        video.merge([Path("a.mp4"), Path("b.mp3")], tmp_path / "joined.mp4")


# ── whisperx ──


def _fake_whisperx(monkeypatch):
    """A fake whisperx: run() imports it lazily; caches and the live path are untouched."""
    import sys
    import types

    from mnema.core import backends

    def load_model(name, device, compute_type=None, model=None, **kw):
        class Pipe:
            def transcribe(self, audio, language=None, progress_callback=None):
                return {"segments": [], "language": language or "ru", "text": ""}

        return Pipe()

    fake = types.SimpleNamespace(
        load_audio=lambda p: b"pcm",
        load_model=load_model,
        load_align_model=lambda language_code, device: (object(), {}),
        align=lambda segs, m, meta, audio, device, progress_callback=None: {"segments": []},
    )
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    monkeypatch.setattr(backends, "shared_whisper_model", lambda: object())
    monkeypatch.setattr(backends, "_WHISPERX_ASR", {})
    return backends


def test_transcribe_does_not_load_alignment(monkeypatch):
    """Word alignment is never called: align downloads a per-language model over the network.
    faster-whisper segments already have times, so they are returned as is (voice messages
    used to hang on align without a network)."""
    backends = _fake_whisperx(monkeypatch)
    import sys

    fake = sys.modules["whisperx"]
    align_called = {"n": 0}

    def spy_align_model(language_code, device):
        align_called["n"] += 1
        raise AssertionError("align must not be called")

    fake.load_align_model = spy_align_model

    def load_model(name, device, compute_type=None, model=None, **kw):
        class Pipe:
            def transcribe(self, audio, language=None, progress_callback=None):
                return {
                    "segments": [{"start": 0.0, "end": 2.0, "text": "привет как дела"}],
                    "language": "ru",
                }

        return Pipe()

    fake.load_model = load_model

    be = backends.WhisperXBackend()
    tr = be.run(Path("voice.ogg"), total_duration=6.0)  # a short voice message

    assert align_called["n"] == 0  # align untouched
    assert [s.text for s in tr.segments] == ["привет как дела"]  # timed segments
    assert tr.segments[0].start == 0.0 and tr.segments[0].end == 2.0
