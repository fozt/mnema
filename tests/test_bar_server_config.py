"""Recognition choices in the app: the Mac lists its own engines without network, Linux the
connected server's list; the app never changes the server configuration."""

import importlib.util
import json
import threading

import httpx
import pytest

from mnema.bar.runtime import BarRuntime
from mnema.bar.state import BarPrefs, asr_options, save_prefs
from mnema.core import base


def _serve(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(handler)}),
    )


_SERVER_ASR = [
    {"id": "auto", "title": "Автоматически"},
    {"id": "gigaam", "title": "GigaAM"},
    {"id": "openai/gpt-4o-transcribe", "title": "OpenAI · gpt-4o-transcribe"},
]


@pytest.mark.parametrize("standalone", [False, True])
def test_asr_options_come_from_the_server_or_this_mac(tmp_path, monkeypatch, standalone):
    """Recognition on the server: Linux gets the connected server's engines with its token,
    without the cloud models, and a saved cloud choice becomes Automatic; On this Mac the Mac
    offers Automatic and its installed engines and never asks the server or providers."""
    monkeypatch.setattr(base, "STANDALONE", standalone)
    monkeypatch.setenv("MNEMA_RUNNER", "http://remote-node:8765")
    monkeypatch.setenv("MNEMA_TOKEN", "remote-token")
    find = importlib.util.find_spec
    engines = {"gigaam": object(), "torch": None, "whisperx": None, "mlx_whisper": None}
    monkeypatch.setattr(  # only GigaAM is installed
        importlib.util,
        "find_spec",
        lambda n, *a, **kw: engines[n] if n in engines else find(n, *a, **kw),
    )
    prefs = tmp_path / "bar.json"
    prefs.write_text(json.dumps({"asr": "openai/gpt-4o-transcribe"}), encoding="utf-8")
    seen = []

    def handler(request):
        if request.url.path == "/api/node/asr":
            seen.append(request)
            return httpx.Response(200, json={"choices": _SERVER_ASR})
        return httpx.Response(200, json={"ok": True, "llm": False, "name": "Домашний"})

    _serve(monkeypatch, handler)
    rt = BarRuntime(prefs_file=prefs)
    thread = rt._load_asr_choices()
    assert thread is not None
    thread.join(3)
    assert not thread.is_alive()
    options = asr_options(rt.menu_env(), rt.prefs.asr)
    assert rt.prefs.asr == "auto"
    if standalone:
        assert seen == []
        assert options == [("auto", "Автоматически"), ("gigaam", "GigaAM")]
    else:
        assert seen and all(r.url.host == "remote-node" for r in seen)
        assert all(r.headers["authorization"] == "Bearer remote-token" for r in seen)
        assert options == [("auto", "Автоматически"), ("gigaam", "GigaAM")]


@pytest.mark.parametrize("server", ["http://remote-node:8765", ""])
def test_cloud_offers_the_cloud_models(tmp_path, monkeypatch, server):
    """In the cloud: with a server its cloud models (Linux and Mac), without one this Mac's cloud
    engine offline; an engine saved before becomes the first cloud model."""
    monkeypatch.setattr(base, "STANDALONE", True)
    monkeypatch.setenv("MNEMA_RUNNER", server)
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/api/node/asr":
            return httpx.Response(200, json={"choices": _SERVER_ASR})
        return httpx.Response(200, json={"ok": True, "llm": True, "name": "Домашний"})

    _serve(monkeypatch, handler)
    prefs = tmp_path / "bar.json"
    prefs.write_text(json.dumps({"recognition": "cloud", "asr": "gigaam"}), encoding="utf-8")
    rt = BarRuntime(prefs_file=prefs)
    thread = rt._load_asr_choices()
    assert thread is not None
    thread.join(3)
    options = asr_options(rt.menu_env(), rt.prefs.asr)
    assert rt.prefs.asr == "openai/gpt-4o-transcribe"
    if server:
        assert options == [("openai/gpt-4o-transcribe", "OpenAI · gpt-4o-transcribe")]
    else:
        assert "/api/node/asr" not in seen
        assert options == [
            ("openai/gpt-4o-transcribe", "OpenAI · gpt-4o-transcribe"),
            ("openai/gpt-4o-mini-transcribe", "OpenAI · gpt-4o-mini-transcribe"),
        ]


def test_linux_without_server_has_no_list(tmp_path, monkeypatch):
    """No server, no list: Linux does not recognize by itself; the saved choice still shows."""
    monkeypatch.setenv("MNEMA_RUNNER", "")
    _serve(monkeypatch, lambda request: pytest.fail("сервера нет — запросов нет"))
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    assert rt._load_asr_choices() is None
    assert asr_options(rt.menu_env(), "whisper") == [("whisper", "whisper")]


def test_unavailable_server_keeps_asr_choice_without_provider_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMA_RUNNER", "http://unavailable-node")
    prefs = tmp_path / "bar.json"
    save_prefs(BarPrefs(recognition="cloud", asr="openai/gpt-4o-mini-transcribe"), prefs)
    seen = []

    def handler(request):
        seen.append(request.url.host)
        raise httpx.ConnectError("offline")

    _serve(monkeypatch, handler)
    rt = BarRuntime(prefs_file=prefs)
    thread = rt._load_asr_choices()
    if thread:
        thread.join(3)
        assert not thread.is_alive()
    assert seen and set(seen) == {"unavailable-node"}
    assert rt.prefs.asr == "openai/gpt-4o-mini-transcribe"
    assert "openai/gpt-4o-mini-transcribe" in dict(asr_options(rt.menu_env(), rt.prefs.asr))


def test_late_asr_list_from_previous_server_cannot_replace_current_choices(tmp_path, monkeypatch):
    # not connected yet: start both reads ourselves and wait for both threads
    rt = BarRuntime(prefs_file=tmp_path / "bar.json")
    started, release = threading.Event(), threading.Event()

    def handler(request):
        if request.url.host == "previous-node":
            started.set()
            assert release.wait(5)
        name = request.url.host
        return httpx.Response(200, json={"choices": [{"id": name, "title": name}]})

    _serve(monkeypatch, handler)
    monkeypatch.setenv("MNEMA_RUNNER", "http://previous-node")
    previous = rt._load_asr_choices()
    assert previous is not None and started.wait(5)
    try:
        monkeypatch.setenv("MNEMA_RUNNER", "http://selected-node")
        selected = rt._load_asr_choices()
        assert selected is not None
        selected.join(5)
        assert not selected.is_alive()
    finally:
        release.set()
        previous.join(5)
    assert not previous.is_alive()
    assert rt.menu_env().asr_choices == [("selected-node", "selected-node")]


@pytest.mark.parametrize(("place", "kept"), [("mac", "auto"), ("server", "gigaam")])
def test_mac_without_gigaam_works_a_saved_gigaam_as_automatic(tmp_path, monkeypatch, place, kept):
    """The Mac app ships no GigaAM: a gigaam choice saved in bar.json works as Automatic on this
    Mac, as a cloud one does, and is not offered; processing on the server keeps it."""
    monkeypatch.setattr(base, "STANDALONE", True)
    monkeypatch.setenv("MNEMA_RUNNER", "http://remote-node:8765")
    find = importlib.util.find_spec
    engines = {"gigaam": None, "silero_vad": None, "mlx_whisper": None, "whisperx": object()}
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda n, *a, **kw: engines[n] if n in engines else find(n, *a, **kw),
    )
    prefs = tmp_path / "bar.json"
    prefs.write_text(json.dumps({"asr": "gigaam", "recognition": place}), encoding="utf-8")
    server = [{"id": "gigaam", "title": "GigaAM"}, {"id": "whisper", "title": "Whisper"}]
    _serve(
        monkeypatch,
        lambda request: (
            httpx.Response(200, json={"choices": server})
            if request.url.path == "/api/node/asr"
            else httpx.Response(200, json={"ok": True, "llm": True, "name": "Домашний"})
        ),
    )
    rt = BarRuntime(prefs_file=prefs)
    thread = rt._load_asr_choices()
    assert thread is not None
    thread.join(3)
    options = dict(asr_options(rt.menu_env(), rt.prefs.asr))
    assert rt.prefs.asr == kept
    assert ("gigaam" in options) is (place == "server")
