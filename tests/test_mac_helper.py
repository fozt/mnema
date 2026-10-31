"""Recognition on the Mac through mnema-asr (core/mac_helper.py): the helper is a real process
(tests/mac_helper_stub.py speaks the shared protocol fixture); texts for people are Python's."""

from __future__ import annotations

import json
import os
import plistlib
import wave
from pathlib import Path

import pytest

from mnema.core import asr_engine, base, mac_helper
from mnema.core.asr_engine import Word
from mnema.core.base import MediaError
from tests.mac_helper_stub import Helper


@pytest.fixture(autouse=True)
def _apple_silicon(monkeypatch):
    monkeypatch.setattr(mac_helper.platform, "machine", lambda: "arm64")


@pytest.fixture
def helper(tmp_path, monkeypatch) -> Helper:
    found = Helper(tmp_path / "app")
    monkeypatch.setenv("MNEMA_ASR", str(found.path))
    return found


def _wav(path: Path, seconds: float = 1.0) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\0\0" * int(16000 * seconds))
    return path


def _bundle(folder: Path, bundle_id: str = "com.mnema.bar") -> Path:
    macos = folder / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "mnema-asr").write_text("#!/bin/sh\n")
    (macos / "mnema-asr").chmod(0o755)
    with open(folder / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, f)
    return folder


def test_words_of_a_track_come_with_their_times_and_progress(helper, tmp_path) -> None:
    seen: list[tuple[float | None, str]] = []
    words = mac_helper.MacHelper().words(
        _wav(tmp_path / "mic.wav"), on=lambda f, m: seen.append((f, m))
    )
    assert words == [
        Word(text="Привет,", start=0.32, end=0.64),
        Word(text="как", start=0.72, end=0.88),
    ]
    assert (None, "Загружаю модель распознавания…") in seen and (0.5, "Распознаю…") in seen
    assert [r.text for r in asr_engine.replies(words)] == ["Привет, как"]


def test_both_tracks_go_in_one_request(helper, tmp_path) -> None:
    wavs = [_wav(tmp_path / "mic.wav"), _wav(tmp_path / "system.wav")]
    tracks = mac_helper.MacHelper().tracks(wavs)
    assert [len(t) for t in tracks] == [2, 0]
    [call] = helper.calls()
    assert call["request"]["payload"]["wavs"] == [str(w) for w in wavs]


def test_on_the_mac_the_recognizer_is_the_helper(helper, monkeypatch) -> None:
    monkeypatch.setattr(base, "STANDALONE", True)
    recognizer = asr_engine.local_recognizer()
    assert recognizer.available() and recognizer.id == "parakeet"
    assert {"ru", "en", "uk"} <= recognizer.langs


def test_a_missing_model_is_downloaded_with_progress_and_the_track_recognized(
    helper, tmp_path
) -> None:
    helper.configure(installed={"parakeet": False, "vad": False})
    seen: list[tuple[float | None, str]] = []
    words = mac_helper.MacHelper().words(
        _wav(tmp_path / "mic.wav"), on=lambda f, m: seen.append((f, m))
    )
    assert len(words) == 2
    assert (0.25, "Скачиваю модель распознавания, только в первый раз…") in seen
    assert helper.config()["installed"]["parakeet"]


def test_models_tell_what_is_downloaded(helper) -> None:
    helper.configure(installed={"parakeet": False})
    [model] = mac_helper.models()
    assert (model.id, model.title, model.installed) == ("parakeet", "Parakeet Ultra", False)
    assert model.size == 633377927
    mac_helper.download("parakeet")
    assert mac_helper.models()[0].installed


@pytest.mark.parametrize(
    ("code", "text"),
    [
        ("no_space", "Не хватает места на диске для модели распознавания"),
        (
            "download_failed",
            "Не удалось скачать модель распознавания. Проверьте подключение к интернету",
        ),
        ("unsupported_mac", "Распознавать на этом Mac можно только с процессором Apple"),
        ("failed", "Не удалось распознать на этом Mac. Подробности в журнале Mnema"),
        ("something_new", "Не удалось распознать на этом Mac. Подробности в журнале Mnema"),
    ],
)
def test_helper_errors_are_told_in_the_users_words(helper, code, text) -> None:
    helper.configure(fail={"download": code})
    with pytest.raises(mac_helper.HelperError) as caught:
        mac_helper.download("parakeet")
    assert str(caught.value) == text


def test_bad_audio_is_told_in_the_users_words(helper, tmp_path) -> None:
    helper.configure(fail={"transcribe": "bad_audio"})
    with pytest.raises(MediaError, match="^Не удалось прочитать звук записи$"):
        mac_helper.MacHelper().words(_wav(tmp_path / "mic.wav"))


@pytest.mark.parametrize(
    ("version", "text"),
    [
        (2, "Обновите команду mnema, чтобы распознавать на этом Mac"),
        (0, "Обновите Mnema, чтобы распознавать на этом Mac"),
    ],
)
def test_another_protocol_version_asks_to_update_the_older_side(helper, version, text) -> None:
    helper.configure(v=version)
    with pytest.raises(MediaError) as caught:
        mac_helper.models()
    assert str(caught.value) == text


def test_the_helper_gets_only_the_allowed_environment(helper, monkeypatch) -> None:
    """FluidAudio reads REGISTRY_URL and proxies from the environment: the helper never sees
    them."""
    monkeypatch.setenv("REGISTRY_URL", "https://mirror.example")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("MNEMA_API_KEY", "sk-secret")
    mac_helper.models()
    [call] = helper.calls()
    assert not {"REGISTRY_URL", "HTTPS_PROXY", "MNEMA_API_KEY"} & set(call["env"])
    assert {"HOME", "PATH", "TMPDIR"} <= set(call["env"])


def test_a_stopped_job_stops_the_helper(helper, tmp_path) -> None:
    helper.configure(installed={"parakeet": False}, hang=True)

    class Stop(Exception):
        pass

    def on(fraction: float | None, message: str) -> None:
        if fraction == 0.25:
            raise Stop

    with pytest.raises(Stop):
        mac_helper.download("parakeet", on=on)
    [call] = helper.calls()
    with pytest.raises(ProcessLookupError):
        os.kill(call["pid"], 0)


def test_speech_is_cut_into_parts_no_longer_than_asked(helper, tmp_path) -> None:
    helper.configure(speech=[[0, 16000], [32000, 32000 + 16000 * 5]])
    spans = mac_helper.MacHelper().speech(_wav(tmp_path / "system.wav", 8), max_s=2.0)
    assert spans[0] == (0, 16000)
    rest = spans[1:]
    assert rest[0][0] == 32000 and rest[-1][1] == 32000 + 16000 * 5
    assert all(e - s <= 32000 for s, e in rest) and len(rest) == 3
    assert all(a[1] == b[0] for a, b in zip(rest, rest[1:], strict=False))


def test_speech_downloads_the_small_speech_model_when_it_is_missing(helper, tmp_path) -> None:
    helper.configure(installed={"parakeet": False, "vad": False})
    spans = mac_helper.MacHelper().speech(_wav(tmp_path / "system.wav"), max_s=150)
    assert spans == [(4096, 20480), (32768, 61440)]
    assert helper.config()["installed"] == {"parakeet": False, "vad": True}


def test_without_the_helper_recognition_asks_for_mnema_or_a_server(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MNEMA_ASR", str(tmp_path / "nowhere" / "mnema-asr"))
    recognizer = mac_helper.MacHelper()
    assert not recognizer.available()
    with pytest.raises(MediaError, match="^Установите Mnema или подключите сервер$"):
        recognizer.words(_wav(tmp_path / "mic.wav"))


def test_an_intel_mac_cannot_recognize_here(helper, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mac_helper.platform, "machine", lambda: "x86_64")
    recognizer = mac_helper.MacHelper()
    assert not recognizer.available()
    with pytest.raises(MediaError, match="только с процессором Apple"):
        recognizer.words(_wav(tmp_path / "mic.wav"))
    assert helper.calls() == []


def test_the_command_finds_the_helper_of_the_installed_app(tmp_path, monkeypatch) -> None:
    """MNEMA_ASR (the app sets it) first, then app.json the app writes, then Mnema.app in
    /Applications or ~/Applications, only with Mnema's bundle identifier."""
    monkeypatch.delenv("MNEMA_ASR")
    monkeypatch.setattr(base, "SYSTEM_APPS", tmp_path / "System" / "Applications")
    assert mac_helper.helper_path() is None

    _bundle(tmp_path / "System" / "Applications" / "Mnema.app", bundle_id="com.example.other")
    assert mac_helper.helper_path() is None
    users = _bundle(Path.home() / "Applications" / "Mnema.app")
    assert mac_helper.helper_path() == users / "Contents/MacOS/mnema-asr"

    placed = _bundle(tmp_path / "Elsewhere" / "Mnema.app")
    app_json = base.app_file()
    app_json.parent.mkdir(parents=True, exist_ok=True)
    asr = placed / "Contents/MacOS/mnema-asr"
    app_json.write_text(json.dumps({"bundle": str(placed), "asr": str(asr), "version": "0.5.0"}))
    assert mac_helper.helper_path() == asr

    explicit = Helper(tmp_path / "dev").path
    monkeypatch.setenv("MNEMA_ASR", str(explicit))
    assert mac_helper.helper_path() == explicit


def test_the_mac_transcribes_a_file_without_a_python_child(helper, tmp_path, monkeypatch) -> None:
    """mnema-asr is the recognition process: the app (or the command) starts it itself; the
    words become replies with the language named from the text."""
    import importlib
    import shutil

    from mnema.core import backends
    from mnema.core.probe import MediaInfo

    # mnema.core exports the transcribe function under the module's name
    transcribe_mod = importlib.import_module("mnema.core.transcribe")

    monkeypatch.setattr(base, "STANDALONE", True)
    src = _wav(tmp_path / "call.wav")
    info = MediaInfo(path=src, duration=1.0, size=1, audio_codec="pcm_s16le", video_codec=None)
    monkeypatch.setattr(transcribe_mod, "probe", lambda path: info)
    monkeypatch.setattr(backends, "extract_audio", lambda s, d, **kw: Path(shutil.copyfile(s, d)))
    result = transcribe_mod.transcribe(src)
    assert (result.text, result.language, result.engine) == ("Привет, как", "ru", "parakeet")
    [call] = helper.calls()
    assert call["ppid"] == os.getpid()


def test_a_track_remark_tells_why_this_mac_could_not_recognize(helper, tmp_path) -> None:
    from mnema.live import remarks

    helper.configure(installed={"parakeet": False}, fail={"download": "no_space"})
    with base.processing_here(), pytest.raises(MediaError) as caught:
        mac_helper.MacHelper().words(_wav(tmp_path / "mic.wav"))
    remark = remarks.asr_failed("mic", caught.value, live_kept=False)
    assert remark.cause == (
        "Не удалось распознать дорожку «Микрофон»: "
        "не хватает места на диске для модели распознавания"
    )
    with base.processing_here():
        crashed = remarks.asr_failed("mic", mac_helper.HelperError("failed"), live_kept=False)
    assert crashed.cause == "Не удалось распознать дорожку «Микрофон»: сбой на Mac"
