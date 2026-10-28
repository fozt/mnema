"""WebSocket contract, and the WLK child is released after end, disconnect or an error."""

import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mnema.devices import DeviceStore, devices_db
from mnema.server import create_app


def test_ws_ready_final_tail_and_process_exit(tmp_path, wlk_process):
    client = TestClient(create_app(tmp_path))
    with client.websocket_connect("/api/live?lang=ru") as ws:
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(b"audio")
        ws.send_json({"type": "end"})
        tail = ws.receive_json()
        assert [s["text"] for s in tail["committed"]] == ["hello"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    _, stopped = wlk_process
    rows = stopped()
    assert rows[0]["lan"] == "ru" and rows[0]["backend"] == "whisper"


@pytest.mark.parametrize("header", [None, "Bearer wrong"])
def test_ws_rejects_bad_token_without_starting_process(monkeypatch, tmp_path, header):
    monkeypatch.setenv("MNEMA_TOKEN", "secret")
    client = TestClient(create_app(tmp_path))
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/api/live?token=secret", headers={} if header is None else {"Authorization": header}
        ),
    ):
        pass


def test_ws_accepts_bearer_token(monkeypatch, tmp_path, wlk_process):
    monkeypatch.setenv("MNEMA_TOKEN", "secret")
    client = TestClient(create_app(tmp_path))
    with client.websocket_connect("/api/live", headers={"Authorization": "Bearer secret"}) as ws:
        assert ws.receive_json() == {"type": "ready"}
    wlk_process[1]()


def test_ws_accepts_device_token_until_revoked(monkeypatch, tmp_path, wlk_process):
    monkeypatch.setenv("MNEMA_TOKEN", "secret")
    client = TestClient(create_app(tmp_path))
    store = DeviceStore(devices_db(tmp_path))
    code, _ = store.create_invite(now=time.time())
    claimed = store.claim(code, "Ноутбук", now=time.time())
    assert claimed is not None
    device_id, token = claimed
    headers = {"Authorization": f"Bearer {token}"}
    with client.websocket_connect("/api/live", headers=headers) as ws:
        assert ws.receive_json() == {"type": "ready"}
    wlk_process[1]()
    assert store.revoke(device_id) is True
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/api/live", headers=headers),
    ):
        pass
    assert refused.value.code == 1008
    assert len(wlk_process[1]()) == 1


def test_ws_disconnect_during_warmup_stops_process(tmp_path, monkeypatch, wlk_process):
    monkeypatch.setenv("MNEMA_TEST_WLK_MODE", "wait")
    client = TestClient(create_app(tmp_path))
    with client.websocket_connect("/api/live"):
        wlk_process[0]()
    wlk_process[1]()


def test_ws_engine_error_does_not_leave_process(tmp_path, monkeypatch, wlk_process):
    monkeypatch.setenv("MNEMA_TEST_WLK_MODE", "fail")
    client = TestClient(create_app(tmp_path))
    with client.websocket_connect("/api/live") as ws:
        error = ws.receive_json()
        assert error["type"] == "error" and "движок не собрался" in error["detail"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    wlk_process[1]()


def test_ws_idle_timeout_stops_process(tmp_path, monkeypatch, wlk_process):
    import mnema.api.live as live

    monkeypatch.setattr(live, "_LIVE_IDLE_TIMEOUT", 1)
    client = TestClient(create_app(tmp_path))
    with client.websocket_connect("/api/live") as ws:
        assert ws.receive_json() == {"type": "ready"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    wlk_process[1]()


def test_ws_pcm_keeps_session_past_idle_timeout(tmp_path, monkeypatch, wlk_process):
    import mnema.api.live as live

    monkeypatch.setattr(live, "_LIVE_IDLE_TIMEOUT", 1)
    client = TestClient(create_app(tmp_path))
    with client.websocket_connect("/api/live") as ws:
        assert ws.receive_json() == {"type": "ready"}
        for _ in range(5):
            ws.send_bytes(b"audio")
            time.sleep(0.4)
        ws.send_json({"type": "end"})
        assert [s["text"] for s in ws.receive_json()["committed"]] == ["hello"]
    wlk_process[1]()


def test_ws_warmup_has_own_limit_instead_of_idle_timeout(tmp_path, monkeypatch, wlk_process):
    import mnema.api.live as live

    monkeypatch.setattr(live, "_LIVE_IDLE_TIMEOUT", 0.1)
    monkeypatch.setenv("MNEMA_TEST_WLK_MODE", "wait")
    client = TestClient(create_app(tmp_path))
    with client.websocket_connect("/api/live") as ws:
        wlk_process[0]()
        time.sleep(0.3)
        monkeypatch.setattr(live, "_LIVE_WARMUP_TIMEOUT", 0)
        error = ws.receive_json()
        assert error["type"] == "error" and "не запустилась" in error["detail"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    wlk_process[1]()


def test_ws_admission_allows_two_tracks_rejects_third_and_reuses_slot(tmp_path, wlk_process):
    client = TestClient(create_app(tmp_path))
    with client.websocket_connect("/api/live") as mic:
        assert mic.receive_json() == {"type": "ready"}
        with client.websocket_connect("/api/live") as system:
            assert system.receive_json() == {"type": "ready"}
            with client.websocket_connect("/api/live") as rejected:
                error = rejected.receive_json()
                assert error["type"] == "error" and "занят" in error["detail"]
                with pytest.raises(WebSocketDisconnect):
                    rejected.receive_json()
            assert len(wlk_process[0](2)) == 2
        with client.websocket_connect("/api/live") as next_track:
            assert next_track.receive_json() == {"type": "ready"}
    wlk_process[1](3)


def test_ws_admission_limit_is_configurable_and_failed_start_releases_slot(
    tmp_path, monkeypatch, wlk_process
):
    monkeypatch.setenv("MNEMA_LIVE_CONNECTIONS", "1")
    monkeypatch.setenv("MNEMA_TEST_WLK_MODE", "fail")
    client = TestClient(create_app(tmp_path))
    with client.websocket_connect("/api/live") as first:
        assert first.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect):
            first.receive_json()
    monkeypatch.delenv("MNEMA_TEST_WLK_MODE")
    with client.websocket_connect("/api/live") as retry:
        assert retry.receive_json() == {"type": "ready"}
    wlk_process[1](2)


def test_ws_without_client_budget_uses_runner_setting(tmp_path, wlk_process, monkeypatch):
    monkeypatch.setenv("MNEMA_ASR_THREADS", "1")
    client = TestClient(create_app(tmp_path))
    with client.websocket_connect("/api/live") as ws:
        assert ws.receive_json() == {"type": "ready"}
    assert wlk_process[1]()[0]["threads"] == 1
