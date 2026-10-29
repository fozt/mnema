"""The server refuses the device token or runs another Mnema version: one notice instead of
sync errors; a refused token also keeps "unavailable" out of the header."""

from mnema import __version__
from mnema.bar.panel import panel_model
from mnema.bar.protocol import SettingsModel
from mnema.bar.state import BarPrefs, BarState, MenuEnv


def model(env: MenuEnv, errors: list[str]):
    state = BarState()
    state.sync_errors = errors
    return panel_model(state, BarPrefs(), env, [], SettingsModel(tabs=[]))


def test_refused_token_says_invitation_and_offers_to_connect():
    env = MenuEnv(server="http://srv:8000", server_ok=False, server_auth=False, server_name="Дом")
    panel = model(env, ["Нужно новое приглашение с сервера"])
    assert panel.where == "Дом: нужно приглашение"
    assert [n.text for n in panel.notices] == [
        "Сервер не принимает это устройство. Нужно новое приглашение с сервера"
    ]
    assert [b.key for b in panel.notices[0].buttons] == ["server_setup"]


def test_unreachable_server_keeps_its_own_words():
    env = MenuEnv(server="http://srv:8000", server_ok=False, server_auth=True, server_name="Дом")
    panel = model(env, ["Сервер не отвечает"])
    assert panel.where == "Дом недоступен"
    assert len(panel.notices) == 1 and panel.notices[0].text.startswith("Синхронизация:")
    assert panel.notices[0].buttons == []


def _release(minor: int) -> str:
    major, ours = map(int, __version__.split(".")[:2])
    return f"{major}.{ours + minor}.0"


def test_newer_server_asks_to_update_the_app_instead_of_sync_errors():
    server = _release(1)
    env = MenuEnv(server="http://srv:8000", server_ok=True, server_version=server, update=server)
    panel = model(env, ["Сервер вернул неполные данные библиотеки"])
    assert [n.text for n in panel.notices] == [
        f"На сервере новая версия Mnema {server}. Обновите приложение"
    ]
    assert [b.key for b in panel.notices[0].buttons] == ["update"]
    # the app's update is not found yet: the same words without a button
    later = model(env.model_copy(update={"update": ""}), [])
    assert [(n.text, n.buttons) for n in later.notices] == [(panel.notices[0].text, [])]


def test_older_server_asks_to_update_the_server():
    major, minor = map(int, __version__.split(".")[:2])
    server = f"{major}.{minor - 1}.9" if minor else f"{major - 1}.99.0"
    env = MenuEnv(server="http://srv:8000", server_ok=True, server_version=server)
    panel = model(env, ["Сервер вернул неполные данные библиотеки"])
    assert [(n.text, n.buttons) for n in panel.notices] == [
        (f"На сервере старая версия Mnema {server}. Обновите сервер", [])
    ]


def test_a_patch_difference_keeps_the_usual_notices():
    major, minor = __version__.split(".")[:2]
    env = MenuEnv(server="http://srv:8000", server_ok=True, server_version=f"{major}.{minor}.99")
    panel = model(env, ["Сервер не отвечает"])
    assert [n.text for n in panel.notices] == ["Синхронизация: сервер не отвечает"]
