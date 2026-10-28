"""Transcription choices of a node: local packages and the models of keys in the node's .env."""

import importlib.util

import httpx
import pytest
from fastapi.testclient import TestClient

from mnema import config, node_settings
from mnema.core.base import MediaError
from mnema.llm import asr, providers
from mnema.llm.providers import Provider
from mnema.server import create_app


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


def _env(*lines: str) -> None:
    """Lines of the node's ~/.config/mnema/.env; the process environment sets no model."""
    path = config.user_env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines))


@pytest.fixture(autouse=True)
def _model_from_env_file(monkeypatch):
    for name in ("MNEMA_MODEL", "MNEMA_API_KEY", "MNEMA_API_BASE", "MNEMA_ASR_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _key(secret="gsk_test_node_asr"):
    """The Groq key is MNEMA_ASR_API_KEY: the node's model is from another provider."""
    _env(
        "MNEMA_MODEL=anthropic/claude-sonnet-5",
        "MNEMA_API_KEY=sk-ant-node",
        f"MNEMA_ASR_API_KEY={secret}",
    )


def test_asr_choices_cache(tmp_path, monkeypatch):
    _key()
    calls = []

    def response(request):
        calls.append(request)
        assert request.extensions["timeout"]["read"] == 5.0
        return httpx.Response(
            200, json={"data": [{"id": "whisper-large-v3"}, {"id": "chat-model"}]}
        )

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(response))
    client = TestClient(create_app(tmp_path))
    expected = [
        {"id": "auto", "title": "Автоматически"},
        {"id": "gigaam", "title": "GigaAM"},
        {"id": "whisper", "title": "Whisper"},
        {"id": "groq/whisper-large-v3", "title": "Groq · whisper-large-v3"},
    ]
    assert client.get("/api/node/asr").json() == {"choices": expected}
    assert client.get("/api/node/asr").json() == {"choices": expected}
    assert len(calls) == 1


def test_asr_cache_expiry_failure_and_key_change(tmp_path, monkeypatch):
    _key("gsk_cache_expiry")
    calls = []
    clock = [100.0]
    monkeypatch.setattr(node_settings.time, "monotonic", lambda: clock[0])

    def response(request):
        calls.append(request)
        if len(calls) > 1:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json={"data": [{"id": "whisper-large-v3"}]})

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(response))
    first = node_settings.asr_choices()
    clock[0] += 601
    assert node_settings.asr_choices() == first
    assert len(calls) == 2
    _key("gsk_replaced_key")
    assert not any(c["id"].startswith("groq/") for c in node_settings.asr_choices())
    assert len(calls) == 3


def test_preflight_local_and_cloud_does_not_access_network(monkeypatch):
    monkeypatch.setattr(
        providers,
        "TRANSPORT",
        httpx.MockTransport(
            lambda request: pytest.fail("Checking parameters must not call the provider")
        ),
    )
    assert node_settings.asr_problem("auto") is None
    assert node_settings.asr_problem("whisper") is None
    assert node_settings.asr_problem("gigaam") is None
    assert node_settings.asr_problem("missing")
    assert node_settings.asr_problem("parakeet")
    assert node_settings.asr_problem("groq/whisper-large-v3")
    assert node_settings.asr_problem("openrouter/model")
    _key("gsk_preflight")
    assert node_settings.asr_problem("groq/whisper-large-v3") is None


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


GROQ_KEY = "gsk_two_providers_groq"
OPENAI_KEY = "sk-proj-two-providers-openai"


_MODELS = {Provider.GROQ: "groq/openai/gpt-oss-120b", Provider.OPENAI: "openai/gpt-5"}


def _keys(model: tuple[Provider, str], asr_key: str | None = None) -> None:
    """The node's model at provider `model` with its key; asr_key is MNEMA_ASR_API_KEY."""
    provider, secret = model
    lines = [f"MNEMA_MODEL={_MODELS[provider]}", f"MNEMA_API_KEY={secret}"]
    _env(*lines, *([f"MNEMA_ASR_API_KEY={asr_key}"] if asr_key else []))


@pytest.mark.parametrize("model_provider", [Provider.OPENAI, Provider.GROQ])
def test_cloud_asr_uses_the_key_of_the_chosen_provider(model_provider):
    """Same transcription provider as the node's model: MNEMA_API_KEY; another one:
    MNEMA_ASR_API_KEY. A Groq model gets the Groq key, an OpenAI model the OpenAI key."""
    keys = {Provider.GROQ: GROQ_KEY, Provider.OPENAI: OPENAI_KEY}
    other = next(p for p in keys if p != model_provider)
    _keys((model_provider, keys[model_provider]), keys[other])
    assert asr.credentials("groq/whisper-large-v3") == ("groq/whisper-large-v3", GROQ_KEY, None)
    assert asr.credentials("openai/whisper-1") == ("openai/whisper-1", OPENAI_KEY, None)
    assert node_settings.asr_problem("groq/whisper-large-v3") is None
    assert node_settings.asr_problem("openai/whisper-1") is None


def test_choices_ask_each_provider_with_its_own_key(monkeypatch):
    _keys((Provider.OPENAI, OPENAI_KEY + "-list"), GROQ_KEY + "-list")
    asked: dict[str, str] = {}

    def response(request):
        asked[request.url.host] = request.headers["authorization"]
        model = "whisper-large-v3" if request.url.host == "api.groq.com" else "whisper-1"
        return httpx.Response(200, json={"data": [{"id": model}]})

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(response))
    cloud = [c["id"] for c in node_settings.asr_choices() if "/" in c["id"]]
    assert cloud == ["groq/whisper-large-v3", "openai/whisper-1"]
    assert asked == {
        "api.groq.com": f"Bearer {GROQ_KEY}-list",
        "api.openai.com": f"Bearer {OPENAI_KEY}-list",
    }


def test_missing_key_of_the_chosen_provider_is_named_and_not_replaced(tmp_path, monkeypatch):
    """Only an OpenAI key (the node's model) and a Groq model chosen: "no Groq key" before the
    uploads are claimed, no job is queued, and the OpenAI key never goes to Groq."""
    _keys((Provider.OPENAI, OPENAI_KEY))
    import litellm

    monkeypatch.setattr(
        litellm, "transcription", lambda **kw: pytest.fail("no other provider is substituted")
    )
    with pytest.raises(MediaError) as missing:
        asr.credentials("groq/whisper-large-v3")
    assert str(missing.value) == "Нет ключа Groq. Выберите другое распознавание"
    assert (
        node_settings.asr_problem("groq/whisper-large-v3")
        == "Нет ключа Groq. Выберите другое распознавание"
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
                "asr": "groq/whisper-large-v3",
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "Нет ключа Groq. Выберите другое распознавание"
    app.state.uploads.claim(upload)  # the job did not claim the upload
    app.state.uploads.release(upload, delete=False)


def test_gateway_key_never_goes_to_the_provider(monkeypatch):
    """An openai/ model behind a gateway or own server (MNEMA_API_BASE): the gateway key is not
    an OpenAI key and is not used for transcription; the OpenAI key is MNEMA_ASR_API_KEY."""
    gateway = "my-private-vllm-token-123"
    _env(
        "MNEMA_MODEL=openai/qwen3-32b",
        "MNEMA_API_BASE=http://my-vllm:8000/v1",
        f"MNEMA_API_KEY={gateway}",
    )
    sent: list[str] = []

    def response(request):
        sent.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": [{"id": "whisper-1"}]})

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(response))
    assert not any("/" in c["id"] for c in node_settings.asr_choices()) and sent == []
    assert (
        node_settings.asr_problem("openai/whisper-1")
        == "Нет ключа OpenAI. Выберите другое распознавание"
    )
    with pytest.raises(MediaError, match="Нет ключа OpenAI. Выберите другое распознавание"):
        asr.credentials("openai/whisper-1")

    _env(
        "MNEMA_MODEL=openai/qwen3-32b",
        "MNEMA_API_BASE=http://my-vllm:8000/v1",
        f"MNEMA_API_KEY={gateway}",
        f"MNEMA_ASR_API_KEY={OPENAI_KEY}",
    )
    assert asr.credentials("openai/whisper-1") == ("openai/whisper-1", OPENAI_KEY, None)
    assert "openai/whisper-1" in [c["id"] for c in node_settings.asr_choices()]
    assert sent == [f"Bearer {OPENAI_KEY}"]


@pytest.mark.parametrize("secret", ["sk-svcacct-" + "s" * 24, "sk-" + "l" * 40])
def test_openai_key_without_project_prefix_is_not_a_groq_key(monkeypatch, secret):
    """An OpenAI key without "sk-proj-" (service or legacy "sk-…") is still OpenAI, not Groq:
    Groq keys always start with "gsk_", so it never goes to api.groq.com."""
    _env(
        "MNEMA_MODEL=gemini/gemini-flash-latest",
        "MNEMA_API_KEY=AIza-node",
        f"MNEMA_ASR_API_KEY={secret}",
    )
    hosts: list[str] = []

    def response(request):
        hosts.append(request.url.host)
        return httpx.Response(200, json={"data": [{"id": "whisper-1"}]})

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(response))
    cloud = [c["id"] for c in node_settings.asr_choices() if "/" in c["id"]]
    assert cloud == ["openai/whisper-1"] and hosts == ["api.openai.com"]
    assert (
        node_settings.asr_problem("groq/whisper-large-v3")
        == "Нет ключа Groq. Выберите другое распознавание"
    )
    assert asr.credentials("openai/whisper-1")[1] == secret
