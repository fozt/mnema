"""Invitations in the app: the "Server" window exchanges a server invitation for a device
token. Server HTTP goes through providers.TRANSPORT (httpx.MockTransport)."""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from dotenv import dotenv_values

from mnema.bar.forms import FormEvent, ServerFlow
from mnema.bar.protocol import FormAsk, FormOp
from mnema.bar.runtime import BarRuntime
from mnema.llm import providers

REFUSED = "Приглашение недействительно: попросите новое на сервере"
CODE = "invite-code-Zq81"
DEVICE_TOKEN = "device-token-Kx42"
INVITE = f"http://node:8123#invite={CODE}"


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    """The test's ~/.config/mnema/.env; the window also writes the process env, teardown
    restores it. Connecting opens the recordings library, its root is in tmp too."""
    for name in ("MNEMA_RUNNER", "MNEMA_TOKEN"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    return tmp_path / ".config/mnema/.env"


class _Node:
    """A node behind MockTransport: the invitation claim and capabilities by device token."""

    def __init__(self, *, claim: int = 200, caps: int = 200) -> None:
        self.claim, self.caps = claim, caps
        self.claims: list[dict[str, Any]] = []
        self.checked: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/devices/claim":
            assert "authorization" not in request.headers  # the exchange has no token
            self.claims.append(json.loads(request.content))
            if self.claim != 200:
                return httpx.Response(self.claim, json={"detail": REFUSED})
            return httpx.Response(200, json={"token": DEVICE_TOKEN, "device": {"id": "d1"}})
        self.checked.append(request.headers.get("authorization"))
        if request.headers.get("authorization") != f"Bearer {DEVICE_TOKEN}":
            return httpx.Response(401)
        if self.caps != 200:
            return httpx.Response(self.caps)
        return httpx.Response(200, json={"ok": True, "llm": False})


def _serve(monkeypatch, node: Callable[[httpx.Request], httpx.Response]) -> None:
    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(node))


def test_invite_is_exchanged_for_a_device_token(monkeypatch, env_file) -> None:
    """Invitation → device token (named after this computer) → a check with it → the address
    and the device token in .env, no invitation code in the file. The window opened again
    does not show the token: the address and "Connected"."""
    node = _Node()
    _serve(monkeypatch, node)
    flow = ServerFlow()
    assert flow.submit({"connection": INVITE}, force=False) is None
    assert node.claims == [{"invite": CODE, "name": socket.gethostname(), "library": True}]
    assert node.checked == [f"Bearer {DEVICE_TOKEN}"]
    env = dotenv_values(env_file)
    assert (env["MNEMA_RUNNER"], env["MNEMA_TOKEN"]) == ("http://node:8123", DEVICE_TOKEN)
    assert CODE not in env_file.read_text() and env_file.stat().st_mode & 0o777 == 0o600
    assert flow.result == (True, True)

    again = ServerFlow().ask()
    assert again.message == "Подключено: node:8123"
    assert [f.id for f in again.fields] == ["connection"]
    assert DEVICE_TOKEN not in again.model_dump_json() and CODE not in again.model_dump_json()


def test_refused_invite_keeps_the_window_and_the_settings(monkeypatch, env_file) -> None:
    """The node refused (403): a red line without "Save anyway", the input stays, .env is
    untouched, no connection check; the text is the app's own, not the node's."""
    env_file.parent.mkdir(parents=True)
    env_file.write_text("MNEMA_RUNNER=http://old:8000\nMNEMA_TOKEN=old-token\n")
    before = env_file.read_text()
    node = _Node(claim=403)
    _serve(monkeypatch, node)
    ask = ServerFlow().submit({"connection": INVITE}, force=False)
    assert ask is not None
    assert (ask.error, ask.force) == (REFUSED, "")
    assert CODE not in ask.model_dump_json()
    assert env_file.read_text() == before and node.checked == []


def test_unreachable_node_does_not_spend_the_invite(monkeypatch, env_file) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет связи", request=request)

    _serve(monkeypatch, down)
    ask = ServerFlow().submit({"connection": INVITE}, force=False)
    assert ask is not None and not ask.force
    assert ask.error == "Сервер не отвечает. Проверьте адрес и запущен ли сервер"
    assert not env_file.exists()


def test_claimed_token_is_kept_when_the_check_fails(monkeypatch, env_file) -> None:
    """An invitation works once: the device token is saved right after the exchange even if
    the check fails; "Save anyway" and a retry never exchange the invitation again."""
    node = _Node(caps=502)
    _serve(monkeypatch, node)
    flow = ServerFlow()
    ask = flow.submit({"connection": INVITE}, force=False)
    assert ask is not None and ask.force == "Сохранить всё равно"
    assert dotenv_values(env_file)["MNEMA_TOKEN"] == DEVICE_TOKEN
    assert flow.submit({"connection": INVITE}, force=False) is not None  # still no answer
    assert flow.submit({"connection": INVITE}, force=True) is None
    assert len(node.claims) == 1
    env = dotenv_values(env_file)
    assert (env["MNEMA_RUNNER"], env["MNEMA_TOKEN"]) == ("http://node:8123", DEVICE_TOKEN)


class _LiveForms:
    """The macOS app with a live form window: every window state goes to a queue."""

    def __init__(self) -> None:
        self.shown: queue.Queue[FormAsk | None] = queue.Queue()
        self.on_event: Callable[[FormEvent | None], None] = lambda ev: None
        self.notes: list[tuple[str, str]] = []

    def open_form(self, ask: FormAsk, on_event: Callable[[FormEvent | None], None]) -> Any:
        self.on_event = on_event
        self.shown.put(ask)
        return self.shown.put

    def notify(self, title: str, message: str, path: Any) -> None:
        self.notes.append((title, message))

    def open(self, path: Any) -> None: ...

    def reveal(self, path: Any) -> None: ...


def test_window_and_log_never_show_the_invite_or_the_token(
    monkeypatch, tmp_path, env_file, caplog
) -> None:
    """The "Server" window in the app: a refusal, then a connection; neither the invitation
    code nor the device token reaches the window states or the log."""
    caplog.set_level(logging.DEBUG)
    node = _Node(claim=403)
    _serve(monkeypatch, node)
    monkeypatch.setenv("MNEMA_LIVE_DIR", str(tmp_path / "notes"))
    app = _LiveForms()
    rt = BarRuntime(platform=app, prefs_file=tmp_path / "bar.json")
    rt.handle("server_setup")
    shown = [app.shown.get(timeout=10)]
    invite = f"http://127.0.0.1:9#invite={CODE}"  # no network for the later check
    app.on_event(FormEvent(op=FormOp.SUBMIT, values={"connection": invite}, rev=1))
    shown += [app.shown.get(timeout=10), app.shown.get(timeout=10)]  # "Checking…", the refusal
    assert shown[-1] is not None and shown[-1].error == REFUSED
    node.claim = 200
    app.on_event(FormEvent(op=FormOp.SUBMIT, values={"connection": invite}, rev=2))
    shown += [app.shown.get(timeout=10), app.shown.get(timeout=10)]
    assert shown[-1] is None  # saved: the window is closed
    for t in threading.enumerate():
        if t.name == "mnema-bar-form":
            t.join(timeout=10)
    assert rt.server == "http://127.0.0.1:9" and rt.server_ok
    texts = [a.model_dump_json() for a in shown if a is not None] + [caplog.text]
    assert not [t for t in texts if CODE in t or DEVICE_TOKEN in t]
