"""A job is seen by whoever queued it: a device sees its own, the node token sees all.

The boundary is the node's HTTP API (TestClient); the operation is a stub in the registry."""

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from mnema import core, ops
from mnema.server import create_app

NODE = "node-token"
VIEW = {"id", "status", "progress", "message", "result", "error", "error_kind"}


class _Params(BaseModel):
    model_config = {"extra": "forbid"}


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A node with a token, two devices and an operation that waits for leave to finish."""
    monkeypatch.setenv("MNEMA_TOKEN", NODE)
    monkeypatch.setattr(core, "probe", lambda path: SimpleNamespace(duration=10.0))
    finish = threading.Event()

    def fn(path, params, on):
        on(None, "Распознаю микрофон…")
        while not finish.wait(0.01):
            on(None, "")
        return {"text": "Готово"}

    app = create_app(tmp_path)
    app.state.operations["slow"] = ops.OpSpec(
        name="slow",
        fn=fn,
        params=_Params,
        input_kind=ops.InputKind.MEDIA,
        output_kind=ops.OutputKind.JSON,
    )
    headers = {"node": {"Authorization": f"Bearer {NODE}"}}
    for name in ("phone", "laptop"):
        code, _ = app.state.devices.create_invite(now=time.time())
        claimed = app.state.devices.claim(code, name, now=time.time())
        assert claimed is not None
        headers[name] = {"Authorization": f"Bearer {claimed[1]}"}
    yield SimpleNamespace(app=app, client=TestClient(app), h=headers, finish=finish, root=tmp_path)
    finish.set()


def _upload(n, who: str) -> str:
    """A device gives a job only its own upload."""
    response = n.client.post(
        "/api/upload", files={"file": ("talk.wav", b"audio")}, headers=n.h[who]
    )
    assert response.status_code == 200, response.text
    return response.json()["path"]


def _submit(n, who: str, **extra) -> str:
    response = n.client.post(
        "/api/jobs", json={"op": "slow", "path": _upload(n, who), **extra}, headers=n.h[who]
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _done(n, who: str, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = n.client.get(f"/api/jobs/{job_id}", headers=n.h[who]).json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.01)
    pytest.fail("the job did not finish")


def _routes(n, who: str, job_id: str) -> dict[str, int]:
    """The status of every job route for this token."""
    c, h = n.client, n.h[who]
    return {
        "get": c.get(f"/api/jobs/{job_id}", headers=h).status_code,
        "cancel": c.post(f"/api/jobs/{job_id}/cancel", headers=h).status_code,
        "delete": c.delete(f"/api/jobs/{job_id}", headers=h).status_code,
    }


def test_other_device_gets_404_on_every_route_of_a_running_job(node):
    """Another device cannot see, cancel or delete the job; it keeps going."""
    job_id = _submit(node, "phone")
    assert _routes(node, "laptop", job_id) == dict.fromkeys(("get", "cancel", "delete"), 404)
    assert node.client.get(f"/api/jobs/{job_id}", headers=node.h["phone"]).json()["status"] in (
        "pending",
        "running",
    )
    node.finish.set()
    job = _done(node, "phone", job_id)
    assert job["status"] == "done" and job["result"] == {"text": "Готово"}
    assert _routes(node, "laptop", job_id) == dict.fromkeys(("get", "cancel", "delete"), 404)
    assert node.client.delete(f"/api/jobs/{job_id}", headers=node.h["phone"]).json() == {"ok": True}


def test_device_cancels_its_own_job(node):
    job_id = _submit(node, "phone")
    cancel = node.client.post(f"/api/jobs/{job_id}/cancel", headers=node.h["phone"])
    assert cancel.status_code == 200 and cancel.json()["ok"] is True
    job = _done(node, "phone", job_id)
    assert (job["status"], job["error_kind"]) == ("error", "cancelled")


def test_node_token_sees_and_cancels_jobs_of_any_device(node):
    job_id = _submit(node, "phone")
    assert node.client.get(f"/api/jobs/{job_id}", headers=node.h["node"]).status_code == 200
    cancel = node.client.post(f"/api/jobs/{job_id}/cancel", headers=node.h["node"])
    assert cancel.status_code == 200 and cancel.json()["ok"] is True
    job = _done(node, "node", job_id)
    assert job["error_kind"] == "cancelled"
    assert node.client.delete(f"/api/jobs/{job_id}", headers=node.h["node"]).status_code == 200


def test_job_of_the_node_token_is_hidden_from_devices(node):
    job_id = _submit(node, "node")
    node.finish.set()
    assert _done(node, "node", job_id)["status"] == "done"
    for device in ("phone", "laptop"):
        assert _routes(node, device, job_id) == dict.fromkeys(("get", "cancel", "delete"), 404)


def test_job_answer_has_no_server_paths(node):
    """A device gets only the job's progress and result: no node paths, parameters or key."""
    job_id = _submit(node, "phone", key="phone-key-1")
    node.finish.set()
    job = _done(node, "phone", job_id)
    assert set(job) == VIEW
    raw = node.client.get(f"/api/jobs/{job_id}", headers=node.h["phone"]).text
    assert str(node.root) not in raw and "phone-key-1" not in raw


def test_retry_key_of_another_device_does_not_reveal_its_job(node):
    """The same retry key from the same device is the same job; another device is refused,
    without the id."""
    job_id = _submit(node, "phone", key="shared-key")
    assert _submit(node, "phone", key="shared-key") == job_id
    other = node.client.post(
        "/api/jobs",
        json={"op": "slow", "path": _upload(node, "laptop"), "key": "shared-key"},
        headers=node.h["laptop"],
    )
    assert other.status_code == 409 and job_id not in other.text
    assert _submit(node, "node", key="shared-key") == job_id  # the node token sees every job
