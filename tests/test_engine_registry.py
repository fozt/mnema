"""A recognition engine is one registered class: the CLI, this machine's Recognition list and the
server's list offer it, and automatic choice takes it for its languages only."""

import importlib.util

import pytest
from fastapi.testclient import TestClient

from mnema import node_settings
from mnema.cli.parser import build_parser
from mnema.core import backends
from mnema.core.base import MediaError
from mnema.core.models import Transcript
from mnema.server import create_app

_REAL_ENGINES = ("torch", "silero_vad", "gigaam", "mlx_whisper", "whisperx", "mlx")


class Klingon(backends.Engine):
    id = name = "klingon"
    title, extra, langs, names_lang = "Klingon ASR", "klingon", frozenset({"tlh"}), False
    model = "qapla-1"

    def available(self) -> bool:
        return True

    def run(self, src, *, lang=None, total_duration=0.0, on=None) -> Transcript:
        return Transcript(text="nuqneH", segments=[], language="tlh")


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    """Only the new engine is installed on this machine."""
    find = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda n, *a, **kw: None if n in _REAL_ENGINES else find(n, *a, **kw),
    )
    monkeypatch.setattr(backends, "ENGINES", (*backends.ENGINES, Klingon))


def test_a_registered_engine_is_offered_by_the_cli_this_machine_and_the_server(tmp_path):
    expected = [{"id": "auto", "title": "Автоматически"}, {"id": "klingon", "title": "Klingon ASR"}]
    assert node_settings.local_asr_choices() == expected
    assert TestClient(create_app(tmp_path)).get("/api/node/asr").json() == {"choices": expected}
    args = build_parser().parse_args(["transcribe", "call.mp3", "--backend", "klingon"])
    assert isinstance(backends.select_backend(args.backend), Klingon)
    assert node_settings.asr_problem("klingon") is None


def test_automatic_choice_takes_a_registered_engine_only_for_its_languages():
    assert isinstance(backends.select_backend("auto", lang="tlh"), Klingon)
    for lang in ("en", None):  # another language; no language, which it cannot name
        with pytest.raises(MediaError):
            backends.select_backend("auto", lang=lang)


def test_a_named_engine_this_machine_lacks_is_refused_before_recognition(tmp_path):
    """The Mac app ships no GigaAM: `--backend gigaam` names what is missing, no module error."""
    from mnema.core.transcribe import transcribe

    with pytest.raises(MediaError) as refused:
        transcribe(tmp_path / "call.wav", backend="gigaam")
    assert str(refused.value) == "Распознавание GigaAM не установлено на этом компьютере"
