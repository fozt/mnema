"""The model and keys of a server user: only her file users/<name>.env on the server, the owner's
too; this machine's .env and the environment outside it.

The settings folder and the server folder are in the test's tmp (conftest).
"""

from __future__ import annotations

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
from mnema.llm.config import LLMSettings, deepl_key, remove_model, save_key, save_model
from mnema.llm.errors import ErrorKind
from mnema.llm.notes import Cloud, LightNote

SAVED = "AIza" + "s" * 31 + "Save"
# keys without a recognizable prefix: only knowing they are keys can hide them
ANNA_KEY = "ak" + "y" * 30 + "z9Y8"
DEEPL = "dk" + "d" * 30 + "q1:fx"


@pytest.fixture(autouse=True)
def _machine_model(monkeypatch):
    """This machine's model as lines of its .env, nothing in the process environment."""
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


def test_on_the_server_a_user_without_a_file_has_no_model() -> None:
    users.set_strict(True)
    settings = _as("anna")
    assert (settings.model, settings.api_key, settings.cloud, settings.source) == (
        None, None, False, "unconfigured",
    )  # fmt: skip


def test_on_the_server_the_model_is_only_the_user_file(monkeypatch) -> None:
    users.set_strict(True)
    _anna("MNEMA_MODEL=deepseek/deepseek-chat", f"MNEMA_API_KEY={ANNA_KEY}")
    for name in ("MNEMA_MODEL", "MNEMA_API_BASE", "MNEMA_ASR_API_KEY"):
        monkeypatch.setenv(name, "from-the-environment")
    settings = _as("anna")
    assert (settings.model, settings.api_key, settings.api_base, settings.asr_api_key) == (
        "deepseek/deepseek-chat", ANNA_KEY, None, None,
    )  # fmt: skip
    assert settings.source == "user"
    _anna("MNEMA_MODEL=deepseek/deepseek-chat")  # a model without a key is no model
    assert _as("anna").cloud is False


def test_on_the_server_the_owner_reads_her_own_file_not_the_shared_env() -> None:
    users.set_strict(True)
    assert _as(users.OWNER).model is None
    _write(
        users.user_env_file(users.OWNER), "MNEMA_MODEL=deepseek/deepseek-chat", "MNEMA_API_KEY=k"
    )
    settings = _as(users.OWNER)
    assert (settings.model, settings.api_key) == ("deepseek/deepseek-chat", "k")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_on_the_server_an_unreadable_file_is_no_model() -> None:
    users.set_strict(True)
    path = _anna("MNEMA_MODEL=deepseek/deepseek-chat", "MNEMA_API_KEY=sk-a")
    path.chmod(0)
    try:
        settings = _as("anna")
    finally:
        path.chmod(0o600)
    assert (settings.model, settings.api_key, settings.cloud) == (None, None, False)


def test_the_server_mode_needs_a_user() -> None:
    users.set_strict(True)
    with pytest.raises(RuntimeError):
        LLMSettings()
    with pytest.raises(RuntimeError):
        deepl_key()


def test_outside_the_server_the_machine_env_and_the_environment_as_before(monkeypatch) -> None:
    _anna(
        "MNEMA_MODEL=deepseek/deepseek-chat", f"MNEMA_API_KEY={ANNA_KEY}", f"DEEPL_API_KEY={DEEPL}"
    )
    for name in (None, "anna"):
        settings = _as(name)
        assert (settings.model, settings.api_key, settings.source) == (
            "gemini/gemini-flash-latest", SAVED, "env",
        )  # fmt: skip
        with users.as_user(name):
            assert deepl_key() is None
    monkeypatch.setenv("MNEMA_MODEL", "openai/gpt-5")
    monkeypatch.setenv("MNEMA_API_KEY", "sk-env")
    assert (_as(None).model, _as(None).api_key) == ("openai/gpt-5", "sk-env")


def test_save_and_remove_touch_only_the_user_file() -> None:
    users.set_strict(True)
    shared = config.user_env_file()
    before = shared.read_bytes()
    environ = dict(os.environ)
    save_model("deepseek/deepseek-chat", ANNA_KEY, user="anna")
    save_key("DEEPL_API_KEY", DEEPL, user="anna")
    path = users.user_env_file("anna")
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    assert shared.read_bytes() == before and dict(os.environ) == environ
    settings = _as("anna")
    assert (settings.model, settings.api_key) == ("deepseek/deepseek-chat", ANNA_KEY)
    with users.as_user("anna"):
        assert deepl_key() == DEEPL
    assert remove_model(user="anna") is True
    assert _as("anna").model is None and shared.read_bytes() == before
    with users.as_user("anna"):
        assert deepl_key() == DEEPL  # the other keys stay
    save_key("DEEPL_API_KEY", None, user="anna")
    with users.as_user("anna"):
        assert deepl_key() is None
    assert remove_model(user="bob") is False
    save_key("MNEMA_ASR_API_KEY", None, user="bob")
    assert not users.user_env_file("bob").exists()


def test_keys_of_every_user_file_are_hidden() -> None:
    redact.redact("warm up the cache")
    _anna(
        "MNEMA_MODEL=deepseek/deepseek-chat", f"MNEMA_API_KEY={ANNA_KEY}", f"DEEPL_API_KEY={DEEPL}"
    )
    for key in (ANNA_KEY, DEEPL):
        assert key not in redact.redact(f"refused: {key}")
    renewed = "ak" + "q" * 30 + "r5T6"
    _anna("MNEMA_MODEL=deepseek/deepseek-chat", f"MNEMA_API_KEY={renewed}")
    assert renewed not in redact.redact(f"refused: {renewed}")


# ── users ─────────────────────────────────────────────────────────────────────


def _server(*names: str) -> DeviceStore:
    store = DeviceStore(devices_db(server_root()))
    for name in names:
        store.add_user(name, now=time.time())
    return store


def _run(*flags: str) -> None:
    args = build_parser().parse_args(list(flags))
    args.func(args)


def test_users_shows_the_model_of_each_file(capsys) -> None:
    _server("anna", "bob")
    _anna("MNEMA_MODEL=deepseek/deepseek-chat", f"MNEMA_API_KEY={ANNA_KEY}")
    _run("users")
    rows = {
        cells[0]: cells[1:]
        for line in capsys.readouterr().out.splitlines()
        if (cells := line.split()) and cells[0] in ("owner", "anna", "bob")
    }
    assert rows == {
        "owner": ["0", "нет", "модели"],
        "anna": ["0", "deepseek/deepseek-chat"],
        "bob": ["0", "нет", "модели"],
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


@pytest.mark.parametrize("name", ["anna", users.OWNER, None])
def test_every_user_fixes_her_model_in_the_mac_settings(name) -> None:
    users.set_strict(name is not None)
    with users.as_user(name):
        assert _hint(ErrorKind.INVALID_KEY).startswith(
            "Замените ключ модели в настройках Mnema на Mac"
        )
        assert _hint(ErrorKind.NO_MODEL).startswith(
            "Выберите другую модель в настройках Mnema на Mac"
        )
        assert _hint(None, cloud=Cloud.NOT_SET).startswith(
            "Задайте модель в настройках Mnema на Mac"
        )
        assert _hint(ErrorKind.NO_MONEY).startswith("Пополните счёт у провайдера модели")
        assert remarks.node_no_model().hint.startswith("Задайте модель в настройках Mnema на Mac")


# ── the server mode at start ─────────────────────────────────────────────────


def test_a_server_in_strict_mode_answers_with_the_model_of_the_asking_user(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from mnema.server import create_app

    monkeypatch.setenv("MNEMA_TOKEN", "node-token-" + "n" * 20)
    users.set_strict(True)
    client = TestClient(create_app(tmp_path / "srv"))
    headers = {"Authorization": "Bearer node-token-" + "n" * 20}
    assert client.get("/api/capabilities", headers=headers).json()["llm"] is False
    save_model("deepseek/deepseek-chat", ANNA_KEY, user=users.OWNER)
    assert client.get("/api/capabilities", headers=headers).json()["llm"] is True
