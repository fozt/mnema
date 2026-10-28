"""Contract of the live CLI command: parser and cmd_live (external parts mocked)."""

from __future__ import annotations

import argparse
import sys
import types

import pytest

import mnema.cli as cli_pkg
import mnema.cli.commands as commands
import mnema.live.asr as asr_mod
import mnema.live.remote as remote_mod
import mnema.live.render as render_mod
import mnema.live.session as session_mod
import mnema.live.sources as sources_mod
import mnema.live.translate as translate_mod
from mnema.cli.parser import build_parser
from mnema.core.base import MediaError
from mnema.core.models import Segment, Transcript
from mnema.live import meta
from mnema.live.models import Track
from mnema.live.setup import _retitle
from mnema.llm import claude


def _temp_recording(monkeypatch, folder):
    import tempfile

    original = tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        prefix = kwargs.get("prefix", args[1] if len(args) > 1 else None)
        if prefix != "mnema-rec-":
            return original(*args, **kwargs)
        folder.mkdir()
        return str(folder)

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)


NODE = "http://node:8123"  # the connected server (MNEMA_RUNNER)


def _no_llm(mp) -> None:
    """Remove the LLM config: no model, so llm_available() is False and optional steps skip."""
    mp.delenv("MNEMA_MODEL", raising=False)


def _patch_live(monkeypatch, captured: dict) -> None:
    from mnema import core
    from mnema.core.models import Transcript
    from tests.node_client import connect

    connect(monkeypatch)
    monkeypatch.setattr(
        core,
        "transcribe",
        lambda *a, **k: Transcript(
            text="привет",
            segments=[Segment(start=0, end=1, text="привет")],
            language="ru",
        ),
    )
    # a connected server (Linux is a client): the node over ASGI; health is network, so it answers
    monkeypatch.setenv("MNEMA_RUNNER", NODE)

    monkeypatch.delenv("MNEMA_LIVE_DIR", raising=False)  # keep artifacts in cwd
    monkeypatch.delenv(
        "DEEPL_API_KEY", raising=False
    )  # the live translator goes the model path in tests
    # the final polished translation (--translate) is an external boundary, no API in tests
    monkeypatch.setattr(
        claude,
        "translate",
        lambda *a, **k: "финальный перевод",
    )
    # sounddevice is an external boundary: CI (Linux) lacks it and cmd_live checks by importing
    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace())

    class FakeMic:
        def __init__(self, *a, **k) -> None:
            self.name = "mic"

        def available(self) -> bool:
            return True

    class FakeView:
        def __init__(self, *a, **k) -> None:
            captured["view"] = type(self).__name__  # which view cmd_live chose
            captured["view_kwargs"] = k

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    class FakePaneView(FakeView): ...

    class FakeSession:
        def __init__(
            self,
            sources,
            factory,
            *,
            translator=None,
            dialog=None,
            view=None,
            recorder=None,
            on=None,
            on_copy=None,
            stop_after_silence=0.0,
            on_started=None,
        ):
            self._on_started = on_started
            captured["on_copy"] = on_copy
            captured["stop_after_silence"] = stop_after_silence
            captured["sources"] = list(sources)
            captured["factory"] = factory
            captured["recorder"] = recorder
            captured["translator"] = translator
            captured["dialog"] = dialog
            self._recorders = recorder if isinstance(recorder, list) else []
            self.asr_failures: dict = captured.get("asr_failures", {})

        def run(self):
            if self._on_started is not None:
                self._on_started()
            # session contract: recorders start at once and close on stop
            for r in self._recorders:
                r.start()
            if captured.get("write_pcm", True):  # "speak" into the WAV tracks (offline pass test)
                # a non-zero signal: the offline pass RMS gate drops segments from silence, so
                # zero PCM would look like a hallucination and the offline text would be lost
                loud = (b"\x10\x27" + b"\xf0\xd8") * 8000  # ±10000, RMS well above the threshold
                for r in self._recorders:
                    w = getattr(r, "write", None)
                    if callable(w):
                        w(loud)
            for r in self._recorders:
                r.stop()
            if "run_tracks" in captured:
                return captured["run_tracks"]
            return [
                Track(
                    label="mic", committed=[Segment(start=0, end=1, text="привет", speaker="mic")]
                )
            ]

    class FakeLocal:
        def __init__(self, **k) -> None:
            captured["kind"] = "local"
            captured.setdefault("labels", []).append(k.get("label"))
            captured.setdefault("langs", {})[k.get("label")] = k.get("lang")

        def warmup(self) -> None: ...

    class FakeRemote:
        def __init__(self, **k) -> None:
            captured["kind"] = "remote"
            captured["runner"] = k.get("runner")
            captured.setdefault("labels", []).append(k.get("label"))
            captured.setdefault("langs", {})[k.get("label")] = k.get("lang")

        def warmup(self) -> None: ...

    monkeypatch.setattr(sources_mod, "MicSource", FakeMic)
    monkeypatch.setattr(render_mod, "LiveView", FakeView)
    monkeypatch.setattr(render_mod, "PaneView", FakePaneView)
    monkeypatch.setattr(session_mod, "LiveSession", FakeSession)
    monkeypatch.setattr(asr_mod, "StreamingTranscriber", FakeLocal)
    monkeypatch.setattr(remote_mod, "RemoteStreamingTranscriber", FakeRemote)
    monkeypatch.setattr(translate_mod, "LiveTranslator", lambda *a, **k: object())


def _args(tmp_path, **over) -> argparse.Namespace:
    """The live command namespace from the parser, so keys and defaults match the CLI (cmd_* read
    attributes directly; a hand-made defaults dict would hide drift)."""
    base = vars(build_parser().parse_args(["live"]))
    base.update(lang="ru", no_record=True, output=str(tmp_path / "live-x"))
    base.update(over)
    return argparse.Namespace(**base)


def test_cmd_live_passes_stop_after_silence_in_seconds(monkeypatch, tmp_path) -> None:
    """The flag is in minutes and reaches the session in seconds (default 5 min -> 300 s)."""
    captured: dict = {}
    _patch_live(monkeypatch, captured)
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)

    commands.cmd_live(_args(tmp_path))  # the parser default, 5.0 min
    assert captured["stop_after_silence"] == 300.0

    commands.cmd_live(_args(tmp_path, stop_after_silence=3.0))
    assert captured["stop_after_silence"] == 180.0


def test_cmd_live_uses_connected_server_without_runner_flag(monkeypatch, tmp_path) -> None:
    captured: dict = {}
    _patch_live(monkeypatch, captured)
    done: dict = {}
    monkeypatch.setattr(commands, "done_panel", lambda p, d: done.update(path=p))

    commands.cmd_live(_args(tmp_path))

    assert captured["kind"] == "remote"
    assert captured["runner"] == NODE
    assert captured["recorder"] is None  # --no-record
    assert done["path"] == tmp_path / "live-x" / "transcript.txt"  # artifacts in the session folder
    assert (tmp_path / "live-x" / "transcript.txt").read_text(encoding="utf-8")


def test_cmd_live_remote_when_runner_flag(monkeypatch, tmp_path) -> None:
    captured: dict = {}
    _patch_live(monkeypatch, captured)
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)

    commands.cmd_live(
        _args(tmp_path, runner="https://203.0.113.10", output=str(tmp_path / "live-r"))
    )

    assert captured["kind"] == "remote"
    assert captured["runner"] == "https://203.0.113.10"


def test_cmd_live_translate_does_not_require_client_api_key(monkeypatch, tmp_path) -> None:
    captured = {}
    _patch_live(monkeypatch, captured)
    _no_llm(monkeypatch)
    commands.cmd_live(_args(tmp_path, translate="en", output=str(tmp_path / "live-y")))
    assert (tmp_path / "live-y" / "transcript.txt").is_file()


def test_failed_translation_hint_names_the_local_transcript(monkeypatch, tmp_path, capsys):
    """Translation on the node failed: the node's remark has no command, and the CLI suggests
    translating the transcript on this disk."""
    import mnema.live.setup as setup_mod

    _patch_live(monkeypatch, {})
    monkeypatch.setattr(setup_mod, "_final_translate", lambda text, **kw: (None, None, True))
    out = tmp_path / "live-t"
    commands.cmd_live(_args(tmp_path, translate="en", output=str(out)))
    printed = capsys.readouterr().out.replace("\n", "")
    assert f"mnema translate {out / 'transcript.txt'} --lang en" in printed
    from mnema.live.meta import last

    run = last(out)
    assert run is not None and "mnema " not in run.model_dump_json()


def test_cmd_live_translate_builds_livedialog(monkeypatch, tmp_path) -> None:
    """`--translate ru`: a two-way dialogue, my language ru, the other side en (auto)."""
    captured: dict = {}
    _patch_live(monkeypatch, captured)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)

    commands.cmd_live(_args(tmp_path, translate="ru", output=str(tmp_path / "live-d")))

    d = captured["dialog"]
    assert d is not None and d.my_lang == "ru" and d.their_lang is None


@pytest.mark.parametrize(
    ("translate", "no_panes", "view"),
    [("ru", False, "FakePaneView"), ("ru", True, "FakeView"), (None, False, "FakeView")],
)
def test_cmd_live_translate_uses_two_pane_view_by_default(
    monkeypatch, tmp_path, translate, no_panes, view
) -> None:
    """--translate: the translation sits alongside, a two-column full-screen view by default;
    --no-panes or no translation gives line output."""
    captured: dict = {}
    _patch_live(monkeypatch, captured)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)

    commands.cmd_live(
        _args(tmp_path, translate=translate, no_panes=no_panes, output=str(tmp_path / "live-p"))
    )

    assert captured["view"] == view


def test_cmd_live_auto_titles_when_no_output(monkeypatch, tmp_path) -> None:
    """Without -o, with a model and text: a dated name from the content; else a timestamp."""
    captured: dict = {}
    _patch_live(monkeypatch, captured)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        claude,
        "title_ex",
        lambda text, **k: ("Дейли про миграцию", "cloud", None),
    )
    done: dict = {}
    monkeypatch.setattr(commands, "done_panel", lambda p, d: done.update(path=p))

    commands.cmd_live(_args(tmp_path, output=None))

    assert done["path"].name == "transcript.txt"  # fixed name inside the session folder
    assert (
        "миграци" in done["path"].parent.name
    )  # the folder carries the meaningful name (date + title)
    assert (
        done["path"].parent.parent == tmp_path / "recordings"
    )  # one subfolder in cwd, not loose files
    assert done["path"].exists()


def test_cmd_live_translate_honors_explicit_lang(monkeypatch, tmp_path) -> None:
    """--lang with --translate is a deliberate quality hint (the call language is known): ASR is
    pinned and the user is told; without --lang it is auto (one engine configuration)."""
    import io

    from rich.console import Console

    captured: dict = {}
    _patch_live(monkeypatch, captured)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)
    buf = io.StringIO()
    monkeypatch.setattr(commands._cli, "console", Console(file=buf, width=200))

    commands.cmd_live(_args(tmp_path, lang="en", translate="ru", output=str(tmp_path / "live-w")))

    assert "закреплён" in buf.getvalue()
    assert captured["langs"] == {"mic": "en"}  # the hint reached ASR


def test_cmd_live_uses_mnema_live_dir_when_no_output(monkeypatch, tmp_path) -> None:
    """Without -o, artifacts go to MNEMA_LIVE_DIR (created), not cwd."""
    captured: dict = {}
    _patch_live(monkeypatch, captured)
    _no_llm(monkeypatch)  # no key: the name stays live-<time>
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    monkeypatch.chdir(tmp_path)
    done: dict = {}
    monkeypatch.setattr(commands, "done_panel", lambda p, d: done.update(path=p))

    commands.cmd_live(_args(tmp_path, output=None))

    assert done["path"].parent.parent == tmp_path / "notes"  # notes/live-<time>/transcript.txt
    assert done["path"].exists()
    assert not list(tmp_path.glob("live-*"))  # nothing landed in cwd


# ── system audio (--system) ──


def _patch_system(monkeypatch, *, ok: bool = True) -> None:
    import mnema.live.system_audio as system_audio_mod

    class FakeSystem:
        def __init__(self, *a, **k) -> None:
            self.name = k.get("label", "system")

        def available(self) -> bool:
            return ok

    # mock the factory (the platform choice of Core Audio tap or parec), not the macOS class:
    # on Linux CI setup takes PulseMonitorSource and a class mock would be bypassed
    monkeypatch.setattr(
        system_audio_mod,
        "system_source",
        lambda *, label="system", tee=None: FakeSystem(label=label),
    )


def test_cmd_live_system_adds_source_and_transcriber_per_source(monkeypatch, tmp_path) -> None:
    """--system: two sources (mic + system), each with its own transcriber (not a shared one)."""
    captured: dict = {}
    _patch_live(monkeypatch, captured)
    _patch_system(monkeypatch)
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)

    commands.cmd_live(_args(tmp_path, system=True))

    mic, system = captured["sources"]
    assert (mic.name, system.name) == ("mic", "system")
    assert captured["labels"] == ["mic", "system"]  # one transcriber built per source
    factory = captured["factory"]
    assert factory(mic) is factory(mic)  # the factory is stable per source...
    assert factory(mic) is not factory(system)  # ...and tells sources apart


def test_cmd_live_system_dialog_pins_by_source(monkeypatch, tmp_path) -> None:
    """--dialog + --system: direction and label follow the source (mic -> "я", system -> "они"),
    whatever the phrase language."""
    captured: dict = {}
    _patch_live(monkeypatch, captured)
    _patch_system(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)

    commands.cmd_live(_args(tmp_path, translate="ru", lang=None, system=True))

    d = captured["dialog"]
    assert set(d.keys()) == {"mic", "system"}
    assert (
        d["mic"].route("How long will it take?")[0] == "я"
    )  # an English phrase, but the source is mic
    assert (
        d["system"].route("Привет, коллеги.")[0] == "они"
    )  # a Russian phrase, but the source is system


@pytest.mark.parametrize(
    ("system", "langs"),
    [
        (True, {"mic": None, "system": None}),  # auto: one engine configuration
        (False, {"mic": None}),  # a lone microphone hears both languages on one channel
    ],
)
def test_cmd_live_system_dialog_sets_lang_per_source(monkeypatch, tmp_path, system, langs) -> None:
    """--dialog + --system: languages are known by source (mic -> mine, system -> theirs); Whisper
    auto-detection only hurt here (an English draft for Russian speech)."""
    captured: dict = {}
    _patch_live(monkeypatch, captured)
    _patch_system(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)

    commands.cmd_live(_args(tmp_path, translate="ru", lang=None, system=system))

    assert captured["langs"] == langs


def test_cmd_live_system_unavailable_raises(monkeypatch, tmp_path) -> None:
    captured: dict = {}
    _patch_live(monkeypatch, captured)
    _patch_system(monkeypatch, ok=False)
    with pytest.raises(MediaError, match="Системный звук"):
        commands.cmd_live(_args(tmp_path, system=True))


# ── the recording comes first (WAV tracks, batch transcription hint) ──


def test_cmd_live_records_wav_track_and_shows_it_in_done(monkeypatch, tmp_path) -> None:
    """Without --no-record there is a WAV track per source (a PCM tee, not a second ffmpeg
    capture): mic.wav exists in the session folder afterwards and is listed in the done output."""
    captured: dict = {}
    _patch_live(monkeypatch, captured)
    done: dict = {}
    monkeypatch.setattr(commands, "done_panel", lambda p, d: done.update(path=p, detail=d))

    commands.cmd_live(_args(tmp_path, no_record=False))

    wav = tmp_path / "live-x" / "mic.wav"
    assert wav.exists()  # the session started and closed the track
    assert "mic.wav" in done["detail"]
    recs = captured["recorder"]
    assert isinstance(recs, list) and len(recs) == 1  # only the WAV track, no ffmpeg audio


def test_retitle_renames_session_folder_keeping_contents(monkeypatch, tmp_path) -> None:
    """_retitle renames the session folder itself; file names inside stay the same."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        claude,
        "title_ex",
        lambda text, **k: ("Дейли про запись", "cloud", None),
    )

    base = tmp_path / "2026-07-02_10-10"
    base.mkdir()
    wav = base / "mic.wav"
    wav2 = base / "system.wav"
    wav.write_bytes(b"x")
    wav2.write_bytes(b"y")

    new_base, paths = _retitle(base, [wav, wav2], "привет, это дейли про запись.")

    assert new_base.name == "2026-07-02_Дейли про запись" and new_base.is_dir()
    assert paths == [new_base / "mic.wav", new_base / "system.wav"]  # names untouched
    assert all(p.exists() for p in paths)
    assert not base.exists()  # the old folder moved as a whole


# ── two passes: the offline pass over the recording after stop (--no-refine) ──


def _fake_setup_dispatch(monkeypatch, calls: list) -> None:
    """dispatch is the boundary of the offline pipeline (engine or runner), mocked in CLI tests."""
    import mnema.live.setup as setup_mod

    def fake(op, src, params, *, runner=None, local=False, on=None):
        calls.append(op)
        return {
            "text": "Пересобранный офлайн текст.",
            "segments": [{"start": 0.0, "end": 2.0, "text": "Пересобранный офлайн текст."}],
            "language": "ru",
        }

    monkeypatch.setattr(setup_mod, "dispatch", fake)


def test_cmd_live_offline_pass_recovers_dead_asr_no_batch_hint(monkeypatch, tmp_path) -> None:
    """ASR died and there is no live text, but the offline pass rebuilt the transcript from the
    recording: the txt has the offline text and no batch transcription hint is needed."""
    import io

    from rich.console import Console

    captured: dict = {
        "run_tracks": [Track(label="mic", committed=[])],
        "asr_failures": {"mic": "движок умер"},
        "write_pcm": True,
    }
    _patch_live(monkeypatch, captured)
    _no_llm(monkeypatch)  # no title, offline only
    calls: list = []
    _fake_setup_dispatch(monkeypatch, calls)
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)
    buf = io.StringIO()
    monkeypatch.setattr(commands._cli, "console", Console(file=buf, width=500))

    commands.cmd_live(_args(tmp_path, no_record=False))

    assert calls == ["transcribe"]
    txt = (tmp_path / "live-x" / "transcript.txt").read_text(encoding="utf-8")
    assert "Пересобранный офлайн текст." in txt
    assert "mnema transcribe" not in buf.getvalue()  # already transcribed by the offline pass


def test_cmd_live_refine_failure_warns_and_keeps_live_text(monkeypatch, tmp_path) -> None:
    """A failed offline pass prints a remark (which track, why, what is left) and the file keeps
    the live text."""
    import io

    from rich.console import Console

    import mnema.live.setup as setup_mod

    captured: dict = {"write_pcm": True}
    _patch_live(monkeypatch, captured)
    _no_llm(monkeypatch)

    def boom(op, src, params, **k):
        raise MediaError("нет движка — задайте --runner")

    monkeypatch.setattr(setup_mod, "dispatch", boom)
    monkeypatch.setattr(commands, "done_panel", lambda p, d: None)
    buf = io.StringIO()
    monkeypatch.setattr(commands._cli, "console", Console(file=buf, width=500))

    commands.cmd_live(_args(tmp_path, no_record=False))

    out = " ".join(buf.getvalue().split())
    assert "Не удалось распознать" in out
    assert "сохранён живой черновик" in out
    from mnema.live.meta import last

    assert last(tmp_path / "live-x").status == "failed"
    assert any("сохранён живой черновик" in r.text for r in last(tmp_path / "live-x").notes)
    assert "привет" in (tmp_path / "live-x" / "transcript.txt").read_text(encoding="utf-8")


def test_cmd_live_unavailable_selected_node_keeps_audio_and_draft(monkeypatch, tmp_path):
    from mnema.client import MnemaClient

    _patch_live(monkeypatch, {})
    monkeypatch.setattr(MnemaClient, "health", lambda self: None)
    commands.cmd_live(_args(tmp_path, runner="http://selected", no_record=False))
    assert (tmp_path / "live-x" / "mic.wav").exists()
    assert "привет" in (tmp_path / "live-x" / "transcript.txt").read_text()


def test_rec_without_server_on_linux_keeps_audio_and_says_so(monkeypatch, tmp_path, capsys):
    """Linux processes only on a server: without one, rec records audio and says the server is
    not connected; the recording stays unprocessed for later."""
    from mnema.bar.recordings import library
    from mnema.live.meta import RecordState

    captured: dict = {"run_tracks": [Track(label="mic", committed=[])]}  # rec: no live text
    _patch_live(monkeypatch, captured)
    monkeypatch.setenv("MNEMA_RUNNER", "")
    monkeypatch.setattr(cli_pkg, "run_streaming", lambda label, fn, **kw: fn(on=lambda *a: None))
    out = tmp_path / "rec-x"
    args = vars(build_parser().parse_args(["rec", "-o", str(out)]))
    args.update(lang="ru")
    with pytest.raises(
        MediaError, match="^Сервер не подключён. Запись сохранена, обработайте позже$"
    ):
        commands.cmd_live(argparse.Namespace(**args))
    assert (out / "mic.wav").is_file()
    assert f"mnema finish {out}" in capsys.readouterr().out.replace("\n", "")
    assert [c.state for c in library(tmp_path).cards] == [RecordState.UNPROCESSED]


def test_rec_no_save_without_server_on_linux_fails_before_recording(monkeypatch, tmp_path):
    """Linux without a server: --no-save has nowhere to send audio and refuses before recording."""
    _patch_live(monkeypatch, {})
    monkeypatch.setenv("MNEMA_RUNNER", "")
    eph = tmp_path / "temporary-recording"
    _temp_recording(monkeypatch, eph)
    monkeypatch.setattr("mnema.live.record.WavTrackWriter.start", lambda *a: pytest.fail("WAV"))
    args = build_parser().parse_args(["rec", "--no-save"])
    with pytest.raises(MediaError, match="^Сервер не подключён. Файлы не сохранены$"):
        commands.cmd_live(args)
    assert not eph.exists()


def test_live_no_mic_uses_system_source_only(monkeypatch, tmp_path):
    """--no-mic --system: system is the only source (the microphone is not opened)."""
    import mnema.live.system_audio as sysaudio_mod

    captured: dict = {}
    _patch_live(monkeypatch, captured)

    class FakeSystem:
        def __init__(self, *a, **k) -> None:
            self.name = "system"

        def available(self) -> bool:
            return True

    monkeypatch.setattr(  # the factory, not the macOS class: Linux CI takes the parec source
        sysaudio_mod, "system_source", lambda *, label="system", tee=None: FakeSystem()
    )
    commands.cmd_live(_args(tmp_path, mic=False, system=True))
    assert [s.name for s in captured["sources"]] == ["system"]


def test_second_ctrl_c_stops_processing_but_keeps_audio(tmp_path, monkeypatch, capsys):
    """A second Ctrl-C during processing stops it and keeps the call: a recording left running for
    a day is trimmed and processed later (mnema finish); it is not listed as unprocessed."""
    captured: dict = {}
    _patch_live(monkeypatch, captured)

    import mnema.live.setup as setup_mod

    def boom_finalize(prepared, *a, **k):
        (prepared.base / "mic.wav").write_bytes(b"\0" * 4096)
        raise KeyboardInterrupt

    monkeypatch.setattr("mnema.live.recording_client.finalize_on_node", boom_finalize)
    monkeypatch.setattr(cli_pkg, "run_streaming", lambda label, fn, **kw: fn(on=lambda *a: None))

    out = tmp_path / "live-x"
    commands.cmd_live(_args(tmp_path, output=str(out), no_record=False))  # does not raise

    assert (out / "mic.wav").exists()
    assert setup_mod.unfinished_sessions(tmp_path) == []
    printed = capsys.readouterr().out + capsys.readouterr().err
    assert "mnema finish" in printed


def test_crashed_processing_keeps_audio_and_says_how_to_finish(tmp_path, monkeypatch, capsys):
    """Processing crashed (an ImportError after git pull mid-recording): the call is safe; print
    where it is and how to finish processing, and only then the traceback."""
    captured: dict = {}
    _patch_live(monkeypatch, captured)

    def boom_finalize(*a, **k):
        raise ImportError("cannot import name 'render_clean'")

    monkeypatch.setattr("mnema.live.recording_client.finalize_on_node", boom_finalize)
    monkeypatch.setattr(cli_pkg, "run_streaming", lambda label, fn, **kw: fn(on=lambda *a: None))

    out = tmp_path / "live-x"
    with pytest.raises(ImportError):
        commands.cmd_live(_args(tmp_path, output=str(out), no_record=False))
    assert out.exists()
    assert f"mnema finish {out}" in capsys.readouterr().out.replace("\n", "")


def test_final_translate_uses_client_credentials_and_reports_failure(monkeypatch):
    import litellm

    import mnema.live.setup as setup_mod

    monkeypatch.setenv("MNEMA_RUNNER", "https://runner")
    monkeypatch.setenv("MNEMA_API_KEY", "client-key")
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        delta = types.SimpleNamespace(content="НАЗВАНИЕ: Имя\n\nперевод")
        choice = types.SimpleNamespace(delta=delta, finish_reason="stop")
        return iter([types.SimpleNamespace(choices=[choice])])

    monkeypatch.setattr(litellm, "completion", completion)
    assert setup_mod._final_translate("text", to_lang="ru", titled=True, on=lambda *a: None) == (
        "перевод",
        "Имя",
        False,
    )
    assert calls[0]["api_key"] == "client-key"
    assert calls[0]["messages"][1]["content"] == "text"

    def failed(**kwargs):
        raise RuntimeError("401 API key not valid")

    monkeypatch.setattr(litellm, "completion", failed)
    assert setup_mod._final_translate("text", to_lang="ru", titled=True, on=lambda *a: None) == (
        None,
        None,
        True,
    )
    monkeypatch.delenv("MNEMA_MODEL")
    assert setup_mod._final_translate("text", to_lang="ru", titled=False, on=lambda *a: None) == (
        None,
        None,
        True,
    )


def test_linux_system_audio_failure_has_no_command_and_logs_why(monkeypatch, caplog):
    """No parec: the message has no commands; what to install goes to the log."""
    import subprocess
    import types as _t

    import mnema.live.system_audio as sa

    def run(cmd, **kw):
        return _t.SimpleNamespace(returncode=0, stdout="alsa_output.usb\n", stderr="")

    def popen(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    with caplog.at_level("WARNING"), pytest.raises(MediaError) as failed:
        sa.PulseMonitorSource(label="system").start()
    assert str(failed.value) == "Не удалось записать системный звук. Подробности в журнале Mnema"
    assert "pulseaudio-utils" in caplog.text


def test_pulse_monitor_source_pumps_pcm(monkeypatch, tmp_path):
    """PulseMonitorSource: the parec stream feeds tee and read(); stop ends the process and closes
    the source with a sentinel."""
    import io
    import types as _t

    import mnema.live.system_audio as sa

    recorded: list[bytes] = []
    src = sa.PulseMonitorSource(label="system", tee=recorded.append)

    def fake_run(cmd, **kw):
        assert cmd[:2] == ["pactl", "get-default-sink"]
        return _t.SimpleNamespace(returncode=0, stdout="alsa_output.usb\n", stderr="")

    pcm = b"\x01\x02" * 1600  # 100 ms of PCM16 at 16 kHz

    class FakeProc:
        def __init__(self):
            self.stdout = io.BytesIO(pcm)
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    proc = FakeProc()

    def fake_popen(cmd, **kw):
        assert "--device=alsa_output.usb.monitor" in cmd  # monitor of the default output
        return proc

    monkeypatch.setattr(sa.subprocess, "run", fake_run)
    monkeypatch.setattr(sa.subprocess, "Popen", fake_popen)

    src.start()
    chunks = []
    while (data := src.read()) is not None:
        if data:
            chunks.append(data)
    assert b"".join(chunks) == pcm  # the whole stream reached the reader
    assert b"".join(recorded) == pcm  # and the recording (tee)

    src.stop()
    assert proc.terminated


@pytest.mark.parametrize("clipboard_ok", [False, True])
def test_cmd_live_no_save_prints_text_when_clipboard_fails(
    monkeypatch, tmp_path, capsys, clipboard_ok
) -> None:
    """--no-save: without a clipboard (ssh, headless, Linux without a tool) the text is printed to
    the terminal so the dictation, the only copy, is never lost; with one, the text is there."""
    captured: dict = {}
    _patch_live(monkeypatch, captured)
    copied: list[str] = []
    monkeypatch.setattr(
        "mnema.live.clipboard.copy_to_clipboard", lambda t: copied.append(t) or clipboard_ok
    )

    commands.cmd_live(_args(tmp_path, no_save=True, output=None))

    out = capsys.readouterr().out
    assert copied == ["привет"]
    if clipboard_ok:
        assert "буфере обмена" in out
    else:
        assert "привет" in out  # the session text itself is in the terminal


@pytest.mark.parametrize("existing_meta", [False, True])
def test_live_no_save_leaves_existing_ephemeral_folder_unchanged(
    monkeypatch, tmp_path, existing_meta
):
    import tempfile

    _patch_live(monkeypatch, {})
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("mnema.live.clipboard.copy_to_clipboard", lambda text: True)
    base = tmp_path / "mnema-live-ephemeral"
    base.mkdir()
    (base / "transcript.txt").write_bytes(b"existing transcript\n")
    if existing_meta:
        (base / ".mnema-meta.json").write_bytes(b'{"custom": "preserve"}\n')
    before = {p.name: p.read_bytes() for p in base.iterdir()}

    commands.cmd_live(_args(tmp_path, no_save=True, output=None))

    assert {p.name: p.read_bytes() for p in base.iterdir()} == before


@pytest.mark.parametrize("no_record", [True, False])
def test_cmd_live_empty_transcript_yellow_panel_not_done(
    monkeypatch, tmp_path, capsys, no_record
) -> None:
    """A session without recognized text (ASR failed or never warmed up, with or without a
    recording): instead of a success panel with an empty file, a yellow "saved without a
    transcript" panel; transcript.txt is not created."""
    captured: dict = {"run_tracks": [Track(label="mic")]}  # no segments
    _patch_live(monkeypatch, captured)
    monkeypatch.setattr("mnema.live.clipboard.copy_to_clipboard", lambda t: False)
    monkeypatch.setattr(
        "mnema.core.transcribe", lambda *a, **k: Transcript(text="", segments=[], language="ru")
    )

    commands.cmd_live(_args(tmp_path, no_record=no_record))

    out = capsys.readouterr().out
    assert "без транскрипта" in out
    assert "✓ готово" not in out  # no green success panel
    assert not (tmp_path / "live-x" / "transcript.txt").exists()


def test_rec_no_save_transcribes_copies_and_cleans_up(monkeypatch, tmp_path, capsys) -> None:
    """rec --no-save: record into a temporary folder, transcribe offline on stop, copy the text,
    delete the folder; nothing appears in recordings/."""
    import mnema.live.setup as setup_mod

    captured: dict = {"write_pcm": True}
    _patch_live(monkeypatch, captured)
    monkeypatch.setattr(
        setup_mod, "_mix_recording", lambda *a: pytest.fail("--no-save must not render MP3")
    )

    eph = tmp_path / "eph"

    _temp_recording(monkeypatch, eph)
    copied: list[str] = []
    monkeypatch.setattr(
        "mnema.live.clipboard.copy_to_clipboard", lambda t: copied.append(t) or True
    )

    def fake_dispatch(op, src, params, *, runner=None, local=False, on=None):
        return {
            "text": "Всё обдумано и решено.",
            "segments": [{"start": 0.0, "end": 2.0, "text": "Всё обдумано и решено."}],
            "language": "ru",
        }

    monkeypatch.setattr(setup_mod, "dispatch", fake_dispatch)
    monkeypatch.chdir(tmp_path)  # recordings/ would be created here

    base = vars(build_parser().parse_args(["rec", "--no-save"]))
    base.update(lang="ru")
    commands.cmd_live(argparse.Namespace(**base))

    out = capsys.readouterr().out
    assert "буфере обмена" in out
    assert copied and "обдумано" in copied[0]  # and in the clipboard
    assert not eph.exists()  # the temporary folder is deleted
    assert not (tmp_path / "recordings").exists()  # nothing left in the working folder
    assert "✓ готово" not in out  # no panel with the deleted folder's path


def test_cmd_live_pane_view_gets_translate_hint(monkeypatch, tmp_path) -> None:
    """The full-screen view gets a hint with the translation directions: the alternate screen hides
    everything printed before, and the meaning of --translate (YOUR language) would be invisible."""
    captured: dict = {}
    _patch_live(monkeypatch, captured)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    commands.cmd_live(_args(tmp_path, translate="en", output=str(tmp_path / "live-p")))

    assert captured["view"] == "FakePaneView"
    hint = captured["view_kwargs"]["hint"]
    assert "я — «en»" in hint and "собеседники — «авто»" in hint and "ВАШ язык" in hint


def test_suppress_pamaccore_keeps_terminal_for_python_stderr() -> None:
    """Regression "empty full-screen view": stderr is a real terminal (pty); inside the filter the
    Python level (sys.stderr, the rich console) must still see a terminal, or rich.Live draws no
    frames until exit. The native fd 2 is filtered and everything else reaches the terminal.
    After exit sys.stderr is restored."""
    import os
    import pty
    import select
    import sys
    import time

    from rich.console import Console

    from mnema.live.stderr_filter import suppress_pamaccore

    master, slave = (
        pty.openpty()
    )  # keep the slave open until read: macOS drops the master buffer after close
    saved_fd2 = os.dup(2)
    os.dup2(slave, 2)
    orig_stderr = sys.stderr
    try:
        with suppress_pamaccore():
            assert sys.stderr.isatty()
            assert Console(stderr=True).is_terminal
            sys.stderr.write("python-line\n")
            sys.stderr.flush()
            os.write(2, b"||PaMacCore (AUHAL)|| Error on line 2523\n")
            os.write(2, b"native-line\n")
            time.sleep(0.3)  # let pump copy
        assert sys.stderr is orig_stderr
    finally:
        os.dup2(saved_fd2, 2)
        os.close(saved_fd2)
    got = b""
    while select.select([master], [], [], 0.2)[0]:
        try:
            chunk = os.read(master, 65536)
        except OSError:  # slave closed: EIO
            break
        if not chunk:
            break
        got += chunk
    os.close(slave)
    os.close(master)
    text = got.decode()
    assert "python-line" in text and "native-line" in text
    assert "PaMacCore" not in text


@pytest.mark.parametrize("standalone", [True, False])
def test_finish_processes_recording_folder(tmp_path, monkeypatch, standalone):
    """`mnema finish <folder>` runs the full final pass over the WAV tracks (after a stop, a crash
    or trimming): the text lands next to the audio and the stopped recording is processed. A Mac
    processes itself, Linux on the connected server."""
    from mnema.core import base as core_base
    from tests.node_client import connect

    connect(monkeypatch)
    monkeypatch.setattr(core_base, "STANDALONE", standalone)
    monkeypatch.setenv("MNEMA_RUNNER", NODE)
    import argparse
    import array
    import wave

    import mnema.live.setup as setup_mod

    base = tmp_path / "live-1"
    base.mkdir()
    with wave.open(str(base / "mic.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(array.array("h", [3000] * 16000).tobytes())
    setup_mod.keep_audio_only(base)
    monkeypatch.setattr(
        setup_mod,
        "dispatch",
        lambda *a, **k: {
            "text": "Итоги.",
            "segments": [{"start": 0.0, "end": 1.0, "text": "Итоги."}],
        },
    )
    _no_llm(monkeypatch)
    monkeypatch.setattr(cli_pkg, "run_streaming", lambda label, fn, **kw: fn(on=lambda *a: None))
    args = argparse.Namespace(path=str(base), lang="ru", runner=None, local=False)
    commands.cmd_finish(args)
    txt = next(tmp_path.glob("*/transcript.txt"))
    assert (meta.last(txt.parent).settings["server"] is None) is standalone
    assert "Итоги." in txt.read_text(encoding="utf-8")
    from mnema.bar.recordings import library
    from mnema.live.meta import RecordState

    assert [c.state for c in library(tmp_path).cards] == [RecordState.PROCESSED]


@pytest.mark.parametrize("mode", ["clipboard-failed", "asr-partial", "asr-failed", "cancelled"])
def test_rec_no_save_never_keeps_temporary_artifacts(monkeypatch, tmp_path, capsys, mode):
    """--no-save never leaves files: not when the clipboard fails, not when ASR fails (on some or
    all tracks), not on cancel; a recognition error reaches the CLI as exit=1."""
    import mnema.live.setup as setup_mod
    import mnema.live.system_audio as sysaudio_mod

    captured = {"write_pcm": True, "run_tracks": [Track(label="mic"), Track(label="system")]}
    _patch_live(monkeypatch, captured)
    eph = tmp_path / "temporary-recording"

    _temp_recording(monkeypatch, eph)
    monkeypatch.setattr(
        sysaudio_mod,
        "system_source",
        lambda **kw: types.SimpleNamespace(name="system", available=lambda: True),
    )
    copied = []
    monkeypatch.setattr(
        "mnema.live.clipboard.copy_to_clipboard", lambda text: copied.append(text) or False
    )

    def dispatch(op, source, params, **kw):
        if (source.stem == "system" and mode == "asr-partial") or mode == "asr-failed":
            raise MediaError("нет движка — задайте --runner")
        return {
            "text": f"AVAILABLE_{source.stem}",
            "language": "ru",
            "segments": [{"start": 0, "end": 1, "text": f"AVAILABLE_{source.stem}"}],
        }

    monkeypatch.setattr(setup_mod, "dispatch", dispatch)
    if mode == "cancelled":

        def cancelled(*args, **kwargs):
            raise KeyboardInterrupt()

        monkeypatch.setattr("mnema.live.recording_client.finalize_on_node", cancelled)
    args = build_parser().parse_args(["rec", "--no-save", "--system", "--lang", "ru"])
    if mode.startswith("asr-"):
        with pytest.raises(MediaError, match="не завершена"):
            commands.cmd_live(args)
    else:
        commands.cmd_live(args)
    out = capsys.readouterr().out
    assert not eph.exists()
    # a temporary recording has no earlier transcript: the recognized half is printed
    assert ("AVAILABLE_mic" in out) is (mode in ("clipboard-failed", "asr-partial"))
    assert "mnema finish" not in out and "звук сохранён" not in out
    assert len(copied) == (1 if mode == "clipboard-failed" else 0)
    if mode.startswith("asr-"):
        assert "Системный звук" in out and "сбой на сервере" in out


def test_recording_error_preserves_normal_session_and_reports_failure(
    monkeypatch, tmp_path, capsys
):
    import mnema.live.setup as setup_mod

    monkeypatch.setattr(
        setup_mod,
        "dispatch",
        lambda *a, **k: {
            "text": "available text",
            "language": "ru",
            "segments": [{"start": 0, "end": 1, "text": "available text"}],
        },
    )
    captured = {"write_pcm": True}
    _patch_live(monkeypatch, captured)
    original = session_mod.LiveSession.run

    def run(self):
        result = original(self)
        self.recording_error = "Не удалось записать mic.wav: disk full"
        return result

    monkeypatch.setattr(session_mod.LiveSession, "run", run)
    with pytest.raises(MediaError, match="Доступный звук и текст сохранены"):
        commands.cmd_live(_args(tmp_path, no_record=False))
    out = capsys.readouterr().out
    assert "disk full" in out and "✓ готово" not in out
    assert (tmp_path / "live-x" / "mic.wav").exists()
    from mnema.live.meta import last

    run = last(tmp_path / "live-x")
    assert run is not None
    assert any(n.step == "record" and "disk full" in n.cause for n in run.notes)


def test_rec_no_save_unavailable_runner_leaves_no_audio(monkeypatch, tmp_path):

    _patch_live(monkeypatch, {})
    eph = tmp_path / "temporary-recording"

    _temp_recording(monkeypatch, eph)
    monkeypatch.setattr("mnema.client.MnemaClient.health", lambda self: None)
    args = build_parser().parse_args(["rec", "--no-save", "--runner", "http://unavailable"])
    with pytest.raises(MediaError, match="Файлы не сохранены"):
        commands.cmd_live(args)
    assert not eph.exists()


def test_no_record_finalizes_server_text_without_wav_or_asr(monkeypatch, tmp_path):
    import mnema.live.setup as setup

    _patch_live(monkeypatch, {})
    monkeypatch.setattr(setup, "dispatch", lambda *a, **kw: pytest.fail("second ASR"))
    monkeypatch.setattr("mnema.live.record.WavTrackWriter.start", lambda *a: pytest.fail("WAV"))
    base = tmp_path / "text-only"
    commands.cmd_live(_args(tmp_path, no_record=True, output=str(base)))
    assert "привет" in (base / "transcript.txt").read_text()
    assert not list(base.glob("*.wav"))
    from mnema.live import meta

    assert meta.last(base).status == "ok"
