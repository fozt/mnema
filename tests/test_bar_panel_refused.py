"""The server refuses the device token: the header and one notice with a button, without
"unavailable"."""

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
