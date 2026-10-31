"""Own model and keys of a server user: the file users/<name>.env next to the shared .env.

Mocked boundaries: input (input, getpass), the provider network (providers.TRANSPORT) and
litellm.completion; the settings folder and the server folder are in the test's tmp (conftest).
"""

from __future__ import annotations

import getpass
import os
import time
from pathlib import Path

import pytest

from mnema import config, users
from mnema.cli.parser import build_parser
from mnema.devices import DeviceStore, devices_db
from mnema.install_setup import server_root
from mnema.live import remarks
from mnema.live.remarks import Step
from mnema.llm import redact
from mnema.llm.config import LLMSettings, remove_model, save_model
from mnema.llm.errors import ErrorKind
from mnema.llm.notes import Cloud, LightNote
from tests import model_stubs

GOOD = "AIza" + "g" * 31 + "Good"
SAVED = "AIza" + "s" * 31 + "Save"
# keys without a recognizable prefix: only knowing they are keys can hide them
SHARED_KEY = "mk" + "x" * 30 + "w1X2"
ANNA_KEY = "ak" + "y" * 30 + "z9Y8"


@pytest.fixture(autouse=True)
def _shared_model(monkeypatch):
    """The shared server model as lines of the shared .env, nothing in the process environment."""
    for name in ("MNEMA_MODEL", "MNEMA_API_KEY", "MNEMA_API_BASE", "MNEMA_ASR_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    _write(
        config.user_env_file(), "MNEMA_MODEL=gemini/gemini-flash-latest", f"MNEMA_API_KEY={SAVED}"
    )


def _write(path: Path, *lines: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


def _anna(*lines: str) -> Path:
    return _write(users.user_env_file("anna"), *lines)


def _as(name: str | None) -> LLMSettings:
    with users.as_user(name):
        return LLMSettings()


def test_a_user_without_a_file_gets_the_shared_model() -> None:
    settings = _as("anna")
    assert (settings.model, settings.api_key, settings.source) == (
        "gemini/gemini-flash-latest", SAVED, "env",
    )  # fmt: skip


def test_own_model_is_the_whole_file_and_the_environment_does_not_reach_it(monkeypatch) -> None:
    _anna("MNEMA_MODEL=deepseek/deepseek-chat", "MNEMA_API_KEY=sk-a")
    monkeypatch.setenv("MNEMA_API_BASE", "http://gw")
    monkeypatch.setenv("MNEMA_ASR_API_KEY", "gsk_x")
    monkeypatch.setenv("MNEMA_MODEL", "anthropic/x")
    settings = _as("anna")
    assert (settings.model, settings.api_key, settings.api_base, settings.asr_api_key) == (
        "deepseek/deepseek-chat", "sk-a", None, None,
    )  # fmt: skip
    assert settings.source == "user"
    assert _as(None).model == "anthropic/x"  # the shared rule is unchanged


def test_a_file_without_a_model_gives_the_shared_one_and_an_unreadable_file_none() -> None:
    _anna("MNEMA_ASR_API_KEY=gsk_" + "a" * 20)
    settings = _as("anna")
    assert (settings.model, settings.source, settings.asr_api_key) == (
        "gemini/gemini-flash-latest", "env", None,
    )  # fmt: skip
    path = users.user_env_file("anna")
    path.unlink()
    path.mkdir()  # a folder instead of the file: unreadable
    settings = _as("anna")
    assert (settings.model, settings.cloud, settings.source) == (None, False, "unconfigured")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_file_takes_no_shared_model() -> None:
    path = _anna("MNEMA_MODEL=deepseek/deepseek-chat", "MNEMA_API_KEY=sk-a")
    path.chmod(0)
    try:
        settings = _as("anna")
    finally:
        path.chmod(0o600)
    assert (settings.model, settings.api_key, settings.cloud) == (None, None, False)


def test_the_owner_file_in_users_is_not_read() -> None:
    _write(
        users.user_env_file(users.OWNER), "MNEMA_MODEL=deepseek/deepseek-chat", "MNEMA_API_KEY=k"
    )
    settings = _as(users.OWNER)
    assert (settings.model, settings.source) == ("gemini/gemini-flash-latest", "env")


def test_the_server_mode_needs_a_user() -> None:
    users.set_strict(True)
    with pytest.raises(RuntimeError):
        LLMSettings()
    assert _as(users.OWNER).model == "gemini/gemini-flash-latest"


def test_save_and_remove_touch_only_the_user_file() -> None:
    shared = config.user_env_file()
    before = shared.read_bytes()
    environ = dict(os.environ)
    save_model("deepseek/deepseek-chat", ANNA_KEY, user="anna")
    path = users.user_env_file("anna")
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    assert shared.read_bytes() == before and dict(os.environ) == environ
    settings = _as("anna")
    assert (settings.model, settings.api_key, settings.source) == (
        "deepseek/deepseek-chat", ANNA_KEY, "user",
    )  # fmt: skip
    _write(path, *path.read_text().splitlines(), "MNEMA_ASR_API_KEY=gsk_" + "b" * 20)
    assert remove_model(user="anna") is True
    assert path.read_text() == "MNEMA_ASR_API_KEY=gsk_" + "b" * 20 + "\n"
    assert _as("anna").source == "env" and shared.read_bytes() == before
    assert remove_model(user="bob") is False


def test_keys_of_every_user_file_are_hidden() -> None:
    _write(
        config.user_env_file(), "MNEMA_MODEL=deepseek/deepseek-chat", f"MNEMA_API_KEY={SHARED_KEY}"
    )
    redact.redact("warm up the cache")
    _anna("MNEMA_MODEL=deepseek/deepseek-chat", f"MNEMA_API_KEY={ANNA_KEY}")
    assert ANNA_KEY not in redact.redact(f"refused: {ANNA_KEY}")
    with users.as_user("anna"):
        assert SHARED_KEY not in redact.redact(f"refused: {SHARED_KEY}")
    renewed = "ak" + "q" * 30 + "r5T6"
    _anna("MNEMA_MODEL=deepseek/deepseek-chat", f"MNEMA_API_KEY={renewed}")
    assert renewed not in redact.redact(f"refused: {renewed}")


# ── mnema model --user ────────────────────────────────────────────────────────


def _server(*names: str) -> DeviceStore:
    store = DeviceStore(devices_db(server_root()))
    for name in names:
        store.add_user(name, now=time.time())
    return store


def _run(*flags: str) -> None:
    args = build_parser().parse_args(list(flags))
    args.func(args)


def _typed(monkeypatch, lines: list[str], secrets: list[str]) -> list[str]:
    asked: list[str] = []

    def ask(queue: list[str]):
        def answer(prompt: str = "") -> str:
            asked.append(prompt)
            if not queue:
                pytest.fail(f"unexpected prompt: {prompt}")
            return queue.pop(0)

        return answer

    monkeypatch.setattr("builtins.input", ask(lines))
    monkeypatch.setattr(getpass, "getpass", ask(secrets))
    return asked


def _said(out: str) -> str:
    return " ".join(out.split())


def test_model_of_a_user_starts_from_her_file_only(monkeypatch, capsys) -> None:
    """The shared model and key are neither preselected nor offered; the result goes to her
    file, the shared .env stays."""
    _server("anna")
    keys = model_stubs.gemini(monkeypatch, GOOD, SAVED)
    model_stubs.Litellm(monkeypatch)
    shared = config.user_env_file().read_bytes()
    asked = _typed(monkeypatch, ["1", ""], [GOOD])
    _run("model", "--user", "Anna")
    assert keys == [GOOD] and not any("оставить" in p for p in asked)
    assert any(p == "Номер: " for p in asked)  # no provider preselected
    assert config.user_env_file().read_bytes() == shared
    assert _as("anna").model == "gemini/gemini-flash-latest" and _as("anna").api_key == GOOD
    assert str(users.user_env_file("anna")) in _said(capsys.readouterr().out)

    _anna("MNEMA_MODEL=gemini/gemini-pro-latest", f"MNEMA_API_KEY={GOOD}")
    asked = _typed(monkeypatch, ["", "3"], [""])
    _run("model", "--user", "anna")
    assert any("[оставить …Good]" in p for p in asked)
    assert _as("anna").model == "gemini/gemini-flash-lite-latest"


def test_model_show_of_a_user_names_shared_or_own(capsys) -> None:
    _server("anna")
    _run("model", "--user", "anna", "--show")
    assert capsys.readouterr().out.strip() == "Общая модель сервера: gemini/gemini-flash-latest"
    _anna("MNEMA_MODEL=deepseek/deepseek-chat", f"MNEMA_API_KEY={ANNA_KEY}")
    _run("model", "--user", "anna", "--show")
    assert capsys.readouterr().out.strip() == "Своя модель: deepseek/deepseek-chat, ключ …z9Y8"
    _run("model", "--user", "anna", "--remove")
    capsys.readouterr()
    _run("model", "--user", "anna", "--show")
    assert capsys.readouterr().out.strip() == "Общая модель сервера: gemini/gemini-flash-latest"


def test_model_of_an_unknown_or_removed_user_is_refused(monkeypatch, capsys) -> None:
    for name in ("anna", "bob"):
        with pytest.raises(SystemExit):
            _run("model", "--user", name, "--show")
        assert f"Нет пользователя {name}. Добавьте: mnema users add {name}" in _said(
            capsys.readouterr().out
        )
    store = _server("anna")
    store.remove_user("anna")
    _typed(monkeypatch, [], [])
    with pytest.raises(SystemExit):
        _run("model", "--user", "anna")
    assert "Нет пользователя anna" in _said(capsys.readouterr().out)
    assert not users.user_env_file("anna").exists()


def test_users_shows_whose_model_is_own(monkeypatch, capsys) -> None:
    _server("anna", "bob")
    _anna("MNEMA_MODEL=deepseek/deepseek-chat", f"MNEMA_API_KEY={ANNA_KEY}")
    _run("users")
    rows = {
        cells[0]: cells[1:]
        for line in capsys.readouterr().out.splitlines()
        if (cells := line.split()) and cells[0] in ("owner", "anna", "bob")
    }
    assert rows == {
        "owner": ["0", "общая"],
        "anna": ["0", "своя:", "deepseek/deepseek-chat"],
        "bob": ["0", "общая"],
    }


def test_users_add_makes_the_library_before_the_user(monkeypatch, capsys) -> None:
    """No user without a library folder: a failed mkdir adds nobody, and adding a user who is
    there already brings a lost folder back."""
    import shutil

    monkeypatch.setenv("MNEMA_TOKEN", "node-token-" + "n" * 20)
    root = server_root()
    store = _server()
    url = "http://node.example:8000"
    (root / "users").write_text("not a folder")
    with pytest.raises(OSError):
        _run("users", "add", "anna", "--url", url)
    assert store.user("anna") is None
    (root / "users").unlink()
    _run("users", "add", "anna", "--url", url)
    library = users.user_library(root, "anna")
    shutil.rmtree(library)
    with pytest.raises(SystemExit):
        _run("users", "add", "anna", "--url", url)
    assert library.is_dir() and library.stat().st_mode & 0o777 == 0o700


# ── remarks for a user of the server ─────────────────────────────────────────


def _hint(kind: ErrorKind | None, *, cloud: Cloud = Cloud.FAILED) -> str:
    note = LightNote(engine="none", cloud=cloud, kind=kind, model="gemini-flash-latest")
    remark = remarks.light(Step.SUMMARY, note)
    assert remark is not None
    return remark.hint


def test_a_user_is_sent_to_the_server_owner() -> None:
    with users.as_user("anna"):
        assert _hint(ErrorKind.INVALID_KEY).startswith("Попросите владельца сервера заменить ключ")
        assert _hint(ErrorKind.NO_MODEL).startswith("Попросите владельца сервера выбрать другую")
        assert _hint(ErrorKind.REGION).startswith("Попросите владельца сервера выбрать другую")
        assert _hint(None, cloud=Cloud.NOT_SET).startswith(
            "Попросите владельца сервера настроить модель"
        )
        assert _hint(ErrorKind.NO_MONEY).startswith("Попросите владельца сервера пополнить счёт")
        assert _hint(ErrorKind.PRIVACY).startswith("Попросите владельца сервера разрешить модель")
        assert remarks.node_no_model().hint.startswith("Попросите владельца сервера настроить")
    for name in (None, users.OWNER):
        with users.as_user(name):
            assert _hint(ErrorKind.INVALID_KEY).startswith("Замените ключ модели на сервере")
            assert _hint(ErrorKind.NO_MONEY).startswith("Пополните счёт у провайдера модели")


def test_a_user_with_own_model_tops_up_her_own_account() -> None:
    _anna("MNEMA_MODEL=deepseek/deepseek-chat", f"MNEMA_API_KEY={ANNA_KEY}")
    with users.as_user("anna"):
        assert _hint(ErrorKind.NO_MONEY).startswith("Пополните счёт у провайдера модели")
        assert _hint(ErrorKind.INVALID_KEY).startswith("Попросите владельца сервера заменить")


# ── the server mode at start ─────────────────────────────────────────────────


def test_a_server_in_strict_mode_starts_and_answers(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from mnema.server import create_app

    monkeypatch.setenv("MNEMA_TOKEN", "node-token-" + "n" * 20)
    users.set_strict(True)
    client = TestClient(create_app(tmp_path / "srv"))
    answer = client.get(
        "/api/capabilities", headers={"Authorization": "Bearer node-token-" + "n" * 20}
    )
    assert answer.status_code == 200 and answer.json()["llm"] is True
