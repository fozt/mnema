"""Users of one server do not see or use each other's jobs, uploads and results.

The boundary is the node's HTTP API (TestClient) with a device of the owner and a device of
anna; operations are stubs in the registry, ffprobe (core.probe) is the external boundary."""

import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from mnema import core, ops, users
from mnema.server import create_app

NODE = "node-token"


class _Params(BaseModel):
    model_config = {"extra": "forbid"}


class _TrackParams(BaseModel):
    tracks: dict[str, str] = {}


def _device(store, user: str) -> dict[str, str]:
    code, _ = store.create_invite(user=user, now=time.time())
    claimed = store.claim(code, f"{user} phone", now=time.time(), library=True)
    assert claimed is not None
    return {"Authorization": f"Bearer {claimed[1]}"}


@pytest.fixture
def srv(tmp_path, monkeypatch):
    """A node with a token, a device of the owner and one of anna; `gate` holds the "wait" op."""
    monkeypatch.setenv("MNEMA_TOKEN", NODE)
    probed: list[Path] = []
    duration = SimpleNamespace(value=10.0)

    def probe(path):
        probed.append(Path(path))
        return SimpleNamespace(duration=duration.value)

    monkeypatch.setattr(core, "probe", probe)
    gate = threading.Event()
    ran: list[str | None] = []

    def who(path, params, on):
        ran.append(users.current_user())
        return {"user": users.current_user()}

    def wait(path, params, on):
        while not gate.wait(0.01):
            on(None, "")
        return {"user": users.current_user()}

    def result_file(path, params, on):
        out = Path(path).with_suffix(".out")
        out.write_text(f"result of {users.current_user()}")
        return {"path": str(out)}

    app = create_app(tmp_path)
    kinds = {
        "who": (who, ops.InputKind.MEDIA, ops.OutputKind.JSON),
        "wait": (wait, ops.InputKind.MEDIA, ops.OutputKind.JSON),
        "file": (result_file, ops.InputKind.MEDIA, ops.OutputKind.FILE),
        "tracks": (who, ops.InputKind.TRACKS, ops.OutputKind.JSON),
    }
    for name, (fn, input_kind, output_kind) in kinds.items():
        params = _Params if input_kind != ops.InputKind.TRACKS else _TrackParams
        app.state.operations[name] = ops.OpSpec(
            name=name, fn=fn, params=params, input_kind=input_kind, output_kind=output_kind
        )
    store = app.state.devices
    store.add_user("anna", now=time.time())
    h = {
        "node": {"Authorization": f"Bearer {NODE}"},
        "owner": _device(store, users.OWNER),
        "anna": _device(store, "anna"),
    }
    (tmp_path / "talk.wav").write_bytes(b"audio")
    yield SimpleNamespace(
        app=app,
        client=TestClient(app, headers={"Accept-Language": "en"}),
        h=h,
        root=tmp_path,
        probed=probed,
        duration=duration,
        gate=gate,
        ran=ran,
    )
    gate.set()


def _upload(n, who: str, name: str = "call.wav", data: bytes = b"audio") -> str:
    response = n.client.post("/api/upload", files={"file": (name, data)}, headers=n.h[who])
    assert response.status_code == 200, response.text
    return response.json()["path"]


def _job(n, who: str, **body):
    return n.client.post("/api/jobs", json=body, headers=n.h[who])


def _done(n, who: str, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = n.client.get(f"/api/jobs/{job_id}", headers=n.h[who]).json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.01)
    pytest.fail("the job did not finish")


def _run(n, who: str, op: str, path: str) -> dict:
    response = _job(n, who, op=op, path=path)
    assert response.status_code == 200, response.text
    return _done(n, who, response.json()["id"])


def test_sync_route_sees_the_user_of_the_request(srv):
    @srv.app.get("/api/test-user")
    def test_user() -> dict:
        return {"user": users.current_user()}

    seen = {who: srv.client.get("/api/test-user", headers=srv.h[who]).json() for who in srv.h}
    assert seen == {
        "node": {"user": users.OWNER},
        "owner": {"user": users.OWNER},
        "anna": {"user": "anna"},
    }


def test_job_runs_as_the_user_who_queued_it(srv):
    for who, user in (("anna", "anna"), ("owner", users.OWNER), ("node", users.OWNER)):
        job = _run(srv, who, "who", _upload(srv, who))
        assert (job["status"], job["result"]) == ("done", {"user": user})


def test_device_input_must_be_an_uploaded_file(srv):
    outside = _job(srv, "anna", op="who", path=str(srv.root / "talk.wav"))
    assert outside.status_code == 403
    assert outside.json()["detail"] == "The input must be an uploaded file"
    track = _job(srv, "anna", op="tracks", tracks={"mic": str(srv.root / "talk.wav")})
    assert track.status_code == 403
    assert track.json()["detail"] == "A track must be an uploaded file"
    assert srv.probed == []
    assert _run(srv, "node", "who", str(srv.root / "talk.wav"))["status"] == "done"


def test_upload_of_another_user_looks_like_a_missing_one(srv):
    """The owner's device gets the same 404 for anna's upload as for a deleted one, before
    ffprobe and admission; anna's upload stays usable."""
    gone = _upload(srv, "owner")
    srv.app.state.uploads.release(gone)
    missing = _job(srv, "owner", op="who", path=gone)
    assert missing.status_code == 404

    annas = _upload(srv, "anna")
    foreign = _job(srv, "owner", op="who", path=annas)
    assert (foreign.status_code, foreign.json()) == (404, missing.json())
    track = _job(srv, "owner", op="tracks", tracks={"mic": annas})
    assert (track.status_code, track.json()) == (404, {**missing.json(), "track": "mic"})
    assert srv.probed == [] and srv.ran == []
    assert _run(srv, "anna", "who", annas)["result"] == {"user": "anna"}


def test_upload_without_owner_mark_is_only_for_the_node_token(srv):
    folder = srv.root / "uploads" / ("a" * 32)
    folder.mkdir(parents=True)
    (folder / "call.wav").write_bytes(b"audio")
    refused = _job(srv, "owner", op="who", path=str(folder / "call.wav"))
    assert refused.status_code == 404 and srv.ran == []
    assert _run(srv, "node", "who", str(folder / "call.wav"))["status"] == "done"


def test_device_cannot_choose_the_transcription_model(srv):
    path = _upload(srv, "anna")
    response = _job(srv, "anna", op="transcribe", path=path, params={"model": "/data/x.bin"})
    assert response.status_code == 422
    assert srv.probed == []


def test_owner_mark_is_not_an_upload_name(srv):
    one = srv.client.post(
        "/api/upload", files={"file": (".mnema-owner", b"anna")}, headers=srv.h["owner"]
    )
    chunked = srv.client.post(
        "/api/uploads", json={"name": ".mnema-owner", "size": 4}, headers=srv.h["owner"]
    )
    assert (one.status_code, chunked.status_code) == (403, 403)


def test_chunked_upload_belongs_to_its_user(srv):
    c = srv.client
    start = c.post("/api/uploads", json={"name": "call.m4a", "size": 5}, headers=srv.h["anna"])
    assert start.status_code == 201
    upload_id = start.json()["id"]
    put = {"params": {"offset": 0}, "content": b"audio"}
    assert c.put(f"/api/uploads/{upload_id}", headers=srv.h["owner"], **put).status_code == 404
    done = c.put(f"/api/uploads/{upload_id}", headers=srv.h["anna"], **put)
    assert done.status_code == 200 and done.json()["offset"] == 5
    assert c.get(f"/api/uploads/{upload_id}", headers=srv.h["owner"]).status_code == 404
    state = c.get(f"/api/uploads/{upload_id}", headers=srv.h["anna"]).json()
    assert state["path"] == done.json()["path"] and Path(state["path"]).name == "call.m4a"
    assert _run(srv, "anna", "who", state["path"])["result"] == {"user": "anna"}


def test_result_file_is_downloaded_only_by_whoever_sees_the_job(srv):
    job = _run(srv, "anna", "file", _upload(srv, "anna"))
    path = job["result"]["path"]
    get = {"params": {"path": path}}
    assert srv.client.get("/api/download", headers=srv.h["owner"], **get).status_code == 404
    mine = srv.client.get("/api/download", headers=srv.h["anna"], **get)
    assert (mine.status_code, mine.text) == (200, "result of anna")
    assert srv.client.get("/api/download", headers=srv.h["node"], **get).status_code == 200
    outside = {"params": {"path": str(srv.root / "talk.wav")}}
    assert srv.client.get("/api/download", headers=srv.h["anna"], **outside).status_code == 403


def test_job_of_a_removed_user_fails_without_running(srv, caplog):
    srv.duration.value = 1000.0  # long: both jobs wait for the one main slot
    caplog.set_level(logging.INFO, logger="mnema.api.jobs")
    first = _job(srv, "owner", op="wait", path=_upload(srv, "owner"))
    queued = _job(srv, "anna", op="who", path=_upload(srv, "anna"))
    assert (first.status_code, queued.status_code) == (200, 200)
    assert srv.app.state.devices.remove_user("anna") is True
    srv.gate.set()
    job = _done(srv, "node", queued.json()["id"])
    assert (job["status"], job["error_kind"], job["error"]) == (
        "error",
        "failed",
        "The user was removed",
    )
    assert srv.ran == []
    annas = [r.getMessage() for r in caplog.records if queued.json()["id"] in r.getMessage()]
    assert len(annas) >= 2 and all("user=anna" in line for line in annas)


def test_job_log_names_the_user(srv, caplog):
    caplog.set_level(logging.INFO, logger="mnema.api.jobs")
    job_id = _job(srv, "anna", op="who", path=_upload(srv, "anna")).json()["id"]
    _done(srv, "anna", job_id)
    lines = [r.getMessage() for r in caplog.records if job_id in r.getMessage()]
    assert len(lines) >= 3 and all("user=anna" in line for line in lines)


def test_one_user_cannot_take_every_waiting_place(srv):
    srv.duration.value = 1000.0
    srv.app.state.job_queue.pending_limit = 2
    assert _job(srv, "owner", op="wait", path=_upload(srv, "owner")).status_code == 200
    assert _job(srv, "anna", op="wait", path=_upload(srv, "anna")).status_code == 200
    assert _job(srv, "anna", op="wait", path=_upload(srv, "anna")).status_code == 429
    assert _job(srv, "owner", op="wait", path=_upload(srv, "owner")).status_code == 200


def test_live_text_takes_devices_of_users_until_removed(srv, wlk_process):
    with srv.client.websocket_connect("/api/live", headers=srv.h["anna"]) as ws:
        assert ws.receive_json() == {"type": "ready"}
    wlk_process[1]()
    assert srv.app.state.devices.remove_user("anna") is True
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        srv.client.websocket_connect("/api/live", headers=srv.h["anna"]),
    ):
        pass
    assert refused.value.code == 1008
