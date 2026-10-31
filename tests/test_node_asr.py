"""Recognition choices of a node: its installed engines, and both OpenAI models for whoever has a
recognition key; the check before work needs no network."""

import importlib.util

import httpx
import pytest
from fastapi.testclient import TestClient

from mnema import config, node_settings
from mnema.llm import providers
from mnema.server import create_app

OPENAI_KEY = "sk-proj-node-asr-openai"
NO_KEY = "Нет ключа OpenAI для распознавания. Задайте его в настройках Mnema на Mac"
LOCAL = [
    {"id": "auto", "title": "Автоматически"},
    {"id": "gigaam", "title": "GigaAM"},
    {"id": "whisper", "title": "Whisper"},
]
CLOUD = [
    {"id": "openai/gpt-4o-transcribe", "title": "OpenAI · gpt-4o-transcribe"},
    {"id": "openai/gpt-4o-mini-transcribe", "title": "OpenAI · gpt-4o-mini-transcribe"},
]


@pytest.fixture(autouse=True)
def local_packages(monkeypatch):
    find = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda n, *a, **kw: (
            object()
            if n in ("gigaam", "whisperx")
            else None
            if n in ("silero_vad", "mlx_whisper")
            else find(n, *a, **kw)
        ),
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    for name in ("MNEMA_MODEL", "MNEMA_API_KEY", "MNEMA_API_BASE", "MNEMA_ASR_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        providers,
        "TRANSPORT",
        httpx.MockTransport(lambda request: pytest.fail("recognition choices ask no provider")),
    )


def _env(*lines: str) -> None:
    """Lines of the node's ~/.config/mnema/.env."""
    path = config.user_env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines))


def test_both_openai_models_are_offered_with_a_recognition_key(tmp_path):
    client = TestClient(create_app(tmp_path))
    assert client.get("/api/node/asr").json() == {"choices": LOCAL}
    _env(f"MNEMA_ASR_API_KEY={OPENAI_KEY}")
    assert client.get("/api/node/asr").json() == {"choices": LOCAL + CLOUD}
    assert node_settings.asr_problem("openai/gpt-4o-mini-transcribe") is None


def test_the_summary_model_key_is_not_a_recognition_key():
    """The recognition key is a key of its own: an OpenAI summary model does not lend its key."""
    _env("MNEMA_MODEL=openai/gpt-5", f"MNEMA_API_KEY={OPENAI_KEY}")
    assert node_settings.asr_choices() == LOCAL
    assert node_settings.asr_problem("openai/gpt-4o-transcribe") == NO_KEY


def test_preflight_checks_packages_and_the_key():
    assert node_settings.asr_problem("auto") is None
    assert node_settings.asr_problem("whisper") is None
    assert node_settings.asr_problem("gigaam") is None
    unavailable = "Выбранное распознавание недоступно. Выберите другое"
    assert node_settings.asr_problem("missing") == unavailable
    assert node_settings.asr_problem("parakeet") == unavailable
    _env(f"MNEMA_ASR_API_KEY={OPENAI_KEY}")
    for retired in ("groq/whisper-large-v3", "openai/whisper-1", "openrouter/model"):
        assert node_settings.asr_problem(retired) == "Неизвестный способ распознавания"


def test_uninstalled_engines_are_not_offered(monkeypatch):
    find = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda n, *a, **kw: (
            None if n in ("gigaam", "whisperx", "silero_vad", "mlx_whisper") else find(n, *a, **kw)
        ),
    )
    assert node_settings.asr_choices() == []
    assert node_settings.asr_problem("auto")


def test_a_cloud_job_without_a_key_is_refused_before_the_uploads(tmp_path, monkeypatch):
    """No recognition key: the job is refused before it claims the uploads, and no other engine
    takes its place."""
    import litellm

    monkeypatch.setattr(
        litellm, "transcription", lambda **kw: pytest.fail("nothing goes to the cloud")
    )
    app = create_app(tmp_path)
    client = TestClient(app)
    upload = client.post("/api/upload", files={"file": ("mic.wav", b"audio")}).json()["path"]
    response = client.post(
        "/api/jobs",
        json={
            "op": "recording",
            "tracks": {"mic": upload},
            "params": {
                "started": "2026-10-23T10:00:00+03:00",
                "name": "2026-10-23_10-00",
                "asr": "openai/gpt-4o-transcribe",
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == NO_KEY
    app.state.uploads.claim(upload)  # the job did not claim the upload
    app.state.uploads.release(upload, delete=False)
