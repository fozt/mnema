"""Resumable chunked uploads (POST/GET/PUT /api/uploads): HTTP contract, no network or ffmpeg."""

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from mnema import ops
from mnema.client import MnemaClient
from mnema.server import create_app

RECORDING = bytes(range(256)) * 40  # 10,240 bytes that do not repeat by chunk
SIZE = len(RECORDING)


class _Params(BaseModel):
    pass


def app_reading_inputs(root: Path, read: list[bytes]):
    """A node with a stub operation that reads the job input, as real transcription does."""

    def op(path, params, on):
        read.append(Path(path).read_bytes())
        return {"ok": True}

    app = create_app(root)
    app.state.operations = {
        "fake": ops.OpSpec(
            name="fake",
            fn=op,
            params=_Params,
            input_kind=ops.InputKind.MEDIA,
            output_kind=ops.OutputKind.JSON,
        )
    }
    return app


def finished(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.005)
    pytest.fail(f"job {job_id} did not finish")


def start(client, name="call.m4a", size=SIZE) -> str:
    response = client.post("/api/uploads", json={"name": name, "size": size})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["offset"] == 0 and len(body["id"]) == 32
    return body["id"]


def put(client, upload_id: str, offset: int, data: bytes):
    return client.put(
        f"/api/uploads/{upload_id}",
        params={"offset": offset},
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )


def raw_put(app, upload_id: str, offset: int, messages: list, *, length: int | None = None):
    """PUT straight over ASGI: the body comes in messages, as from uvicorn, possibly cut off.

    None in messages means the client went quiet: receive returns nothing more."""
    headers = [(b"content-type", b"application/octet-stream")]
    if length is not None:
        headers.append((b"content-length", str(length).encode()))
    path = f"/api/uploads/{upload_id}"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": f"offset={offset}".encode(),
        "root_path": "",
        "headers": headers,
        "client": ("phone", 50000),
        "server": ("node", 8000),
    }
    pending = list(messages)

    async def receive():
        if not pending:
            return {"type": "http.disconnect"}
        message = pending.pop(0)
        if message is None:
            await asyncio.Event().wait()
        return message

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def call():
        await asyncio.wait_for(app(scope, receive, send), 5)
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return status, json.loads(body)

    return call()


def piece(data: bytes, *, more: bool = True) -> dict:
    return {"type": "http.request", "body": data, "more_body": more}


def test_three_chunks_assemble_the_file_and_a_job_accepts_it(tmp_path):
    read: list[bytes] = []
    client = TestClient(app_reading_inputs(tmp_path, read))
    upload_id = start(client)
    chunks = [RECORDING[:4096], RECORDING[4096:8192], RECORDING[8192:]]
    offset, answers = 0, []
    for data in chunks:
        response = put(client, upload_id, offset, data)
        assert response.status_code == 200, response.text
        answers.append(response.json())
        offset = response.json()["offset"]
    assert [a["offset"] for a in answers] == [4096, 8192, len(RECORDING)]
    assert [a["path"] for a in answers[:2]] == [None, None]
    path = Path(answers[-1]["path"])
    assert path.is_absolute() and path.name == "call.m4a"
    assert path.parent == (tmp_path / "uploads" / upload_id).resolve()
    assert path.read_bytes() == RECORDING
    assert [p.name for p in path.parent.iterdir()] == ["call.m4a"]  # the marker is gone
    info = client.get(f"/api/uploads/{upload_id}").json()
    assert info == {
        "id": upload_id,
        "name": "call.m4a",
        "size": len(RECORDING),
        "offset": len(RECORDING),
        "path": str(path),
    }
    job = client.post("/api/jobs", json={"op": "fake", "path": str(path)})
    assert job.status_code == 200, job.text
    assert finished(client, job.json()["id"])["status"] == "done"
    assert read == [RECORDING]


def test_wrong_offset_is_a_conflict_with_the_current_offset(tmp_path):
    client = TestClient(create_app(tmp_path))
    upload_id = start(client)
    put(client, upload_id, 0, RECORDING[:4096])
    for stale in (0, 5000):
        response = put(client, upload_id, stale, RECORDING[stale : stale + 100])
        assert response.status_code == 409
        assert response.json()["offset"] == 4096
        assert response.json()["detail"]
    assert client.get(f"/api/uploads/{upload_id}").json()["offset"] == 4096


def test_chunk_past_the_declared_size_is_rejected_and_nothing_is_kept(tmp_path):
    client = TestClient(create_app(tmp_path))
    upload_id = start(client, size=10)
    assert put(client, upload_id, 0, b"x" * 11).status_code == 400
    # a body without Content-Length: the size shows only while reading
    response = client.put(
        f"/api/uploads/{upload_id}", params={"offset": 0}, content=iter([b"x" * 6, b"y" * 6])
    )
    assert response.status_code == 400
    assert client.get(f"/api/uploads/{upload_id}").json()["offset"] == 0
    assert put(client, upload_id, 0, b"z" * 10).json()["offset"] == 10


def test_overflow_in_a_later_part_of_the_body_rolls_the_chunk_back(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    upload_id = start(client, size=10)
    put(client, upload_id, 0, b"ab")
    status, _ = asyncio.run(
        raw_put(app, upload_id, 2, [piece(b"cdef"), piece(b"ghijk", more=False)])
    )
    assert status == 400
    assert client.get(f"/api/uploads/{upload_id}").json()["offset"] == 2


def test_interrupted_chunk_keeps_what_arrived_and_the_next_put_continues(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    upload_id = start(client)
    put(client, upload_id, 0, RECORDING[:4096])
    # the client vanished mid-second chunk: 1500 of 4096 bytes arrived
    asyncio.run(
        raw_put(
            app,
            upload_id,
            4096,
            [piece(RECORDING[4096:5596]), {"type": "http.disconnect"}],
            length=4096,
        )
    )
    info = client.get(f"/api/uploads/{upload_id}").json()
    file = tmp_path / "uploads" / upload_id / "call.m4a"
    assert info["offset"] == file.stat().st_size == 5596 and info["path"] is None
    response = put(client, upload_id, 5596, RECORDING[5596:])
    assert response.json()["offset"] == len(RECORDING)
    assert Path(response.json()["path"]).read_bytes() == RECORDING


def test_one_upload_takes_one_put_at_a_time_and_a_silent_client_releases_it(tmp_path, monkeypatch):
    import mnema.api.files as files

    monkeypatch.setattr(files, "CHUNK_STALL_S", 1.0)
    app = create_app(tmp_path)
    client = TestClient(app)
    first, second = start(client), start(client, name="system.m4a")

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://node") as http:
            stalled = asyncio.create_task(
                raw_put(app, first, 0, [piece(RECORDING[:700]), None], length=4096)
            )
            deadline = time.monotonic() + 3
            while (await http.get(f"/api/uploads/{first}")).json()["offset"] < 700:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            busy = await http.put(f"/api/uploads/{first}", params={"offset": 700}, content=b"x")
            other = await http.put(f"/api/uploads/{second}", params={"offset": 0}, content=b"y")
            await stalled
            resumed = await http.put(
                f"/api/uploads/{first}", params={"offset": 700}, content=RECORDING[700:]
            )
            return busy, other, resumed

    busy, other, resumed = asyncio.run(scenario())
    assert busy.status_code == 409 and busy.json()["offset"] == 700
    assert other.status_code == 200 and other.json()["offset"] == 1
    assert resumed.status_code == 200
    assert Path(resumed.json()["path"]).read_bytes() == RECORDING


def test_job_on_an_unfinished_upload_is_a_conflict(tmp_path):
    read: list[bytes] = []
    client = TestClient(app_reading_inputs(tmp_path, read))
    upload_id = start(client)
    put(client, upload_id, 0, RECORDING[:100])
    path = tmp_path / "uploads" / upload_id / "call.m4a"
    response = client.post("/api/jobs", json={"op": "fake", "path": str(path)})
    assert response.status_code == 409
    assert response.json()["detail"] == "Загрузка не завершена"
    assert read == []
    done = put(client, upload_id, 100, RECORDING[100:]).json()
    job = client.post("/api/jobs", json={"op": "fake", "path": done["path"]})
    assert finished(client, job.json()["id"])["status"] == "done"
    assert read == [RECORDING]


def test_sweep_removes_only_unfinished_uploads_idle_for_more_than_a_day(tmp_path):
    client = TestClient(create_app(tmp_path))
    stale, fresh, revived = start(client), start(client), start(client)
    for upload_id in (stale, fresh, revived):
        put(client, upload_id, 0, RECORDING[:10])
    uploads = tmp_path / "uploads"
    two_days, half_day = time.time() - 2 * 86400, time.time() - 43200
    os.utime(uploads / stale, (two_days, two_days))
    os.utime(uploads / fresh, (half_day, half_day))
    os.utime(uploads / revived, (two_days, two_days))
    put(client, revived, 10, RECORDING[10:20])  # the TTL counts from the last chunk
    start(client)  # a new upload sweeps forgotten ones
    assert client.get(f"/api/uploads/{stale}").status_code == 404
    assert client.get(f"/api/uploads/{fresh}").json()["offset"] == 10
    assert client.get(f"/api/uploads/{revived}").json()["offset"] == 20
    assert put(client, stale, 10, RECORDING[10:20]).status_code == 404


def test_name_must_be_a_plain_file_name_and_size_is_bounded(tmp_path):
    client = TestClient(create_app(tmp_path))
    for name in ("..", "", ".partial.json", "a\x00b.m4a", "x" * 256):
        response = client.post("/api/uploads", json={"name": name, "size": 10})
        assert response.status_code == 403, name
    assert not list((tmp_path / "uploads").glob("*"))
    upload_id = start(client, name="../../etc/call.m4a", size=3)
    path = put(client, upload_id, 0, b"abc").json()["path"]
    assert Path(path) == (tmp_path / "uploads" / upload_id / "call.m4a").resolve()
    for size in (0, -1, 20 * 1024**3 + 1):
        response = client.post("/api/uploads", json={"name": "call.m4a", "size": size})
        assert response.status_code == 422, size
    start(client, size=20 * 1024**3)


def test_unknown_upload_is_not_found(tmp_path):
    client = TestClient(create_app(tmp_path))
    for upload_id in ("0" * 32, "Z" * 32, "not-an-id"):
        assert client.get(f"/api/uploads/{upload_id}").status_code == 404
        assert put(client, upload_id, 0, b"x").status_code == 404


class LosesOneResponse(httpx.AsyncBaseTransport):
    """The network to the node: the request arrives but the answer to the chosen PUT is lost."""

    def __init__(self, app, lose_put: int):
        self._node = httpx.ASGITransport(app=app)
        self._lose_put = lose_put
        self.puts: list[int] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._node.handle_async_request(request)
        if request.method == "PUT":
            await response.aread()
            self.puts.append(response.status_code)
            if len(self.puts) == self._lose_put:
                raise httpx.ReadError("mobile network dropped", request=request)
        return response


def test_client_resumes_after_a_lost_response_from_the_offset_in_409(tmp_path):
    read: list[bytes] = []
    app = app_reading_inputs(tmp_path / "node", read)
    src = tmp_path / "call.m4a"
    src.write_bytes(RECORDING)
    transport = LosesOneResponse(app, lose_put=2)
    client = MnemaClient("http://node", transport=transport)
    client._retry_backoff = 0
    path = client.upload_resumable(src, chunk=4096)
    assert Path(path).read_bytes() == RECORDING
    assert transport.puts == [200, 200, 409, 200]
    node = TestClient(app)
    job = node.post("/api/jobs", json={"op": "fake", "path": path})
    assert finished(node, job.json()["id"])["status"] == "done"
    assert read == [RECORDING]
