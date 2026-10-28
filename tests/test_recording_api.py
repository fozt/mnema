"""Recording job contract from the fixture shared with the phone (recording-api-v1.json): the
client request, the server's job answers, the folder after applying the result, and how the
client reacts to the answers."""

import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mnema import ops
from mnema.api.models import JobRequest
from mnema.client import MnemaClient
from mnema.core.base import MediaError
from mnema.devices import DeviceStore
from mnema.jobs import JobView
from mnema.live import meta
from mnema.live.recording_client import own_title, process, set_own_title
from mnema.server import create_app

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "recording-api-v1.json").read_text(encoding="utf-8")
)
STARTED = "2026-10-23T12:30:00+03:00"


class Node:
    """The server over HTTP: chunked uploads and jobs; submit and poll answers follow the
    scenario, the rest are ordinary."""

    def __init__(self, result, *, submit=(), poll=()):
        self.result, self.submit, self.poll = result, list(submit), list(poll)
        self.uploads: list[str] = []
        self.keys: list[str] = []
        self.client = MnemaClient("http://node", transport=httpx.MockTransport(self.handle))
        self.client._retry_backoff = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if method == "POST" and path == "/api/uploads":
            body = json.loads(request.content)
            self.uploads.append(Path(body["name"]).stem)
            upload = f"u{len(self.uploads)}"
            return httpx.Response(200, json={"id": upload, **body, "offset": 0})
        if method == "PUT" and path.startswith("/api/uploads/"):
            upload = path.rsplit("/", 1)[1]
            done = {"offset": len(request.content), "path": f"uploads/{upload}/audio"}
            return httpx.Response(200, json=done)
        if method == "POST" and path == "/api/jobs":
            self.keys.append(json.loads(request.content)["key"])
            reply = self.submit.pop(0) if self.submit else None
            if reply is None:
                return httpx.Response(200, json={"id": f"j{len(self.keys)}"})
            return httpx.Response(reply["status"], json=reply["body"])
        if method == "GET" and path.startswith("/api/jobs/"):
            reply = self.poll.pop(0) if self.poll else None
            if reply is None:
                job = path.rsplit("/", 1)[1]
                return httpx.Response(
                    200, json={"id": job, "status": "done", "result": self.result}
                )
            return httpx.Response(reply["status"], json=reply["body"])
        if method in ("DELETE", "POST") and path.startswith("/api/jobs/"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": "нет такого"})


def _folder(root: Path, name: str, files: dict[str, str]) -> Path:
    base = root / name
    base.mkdir()
    for label in ("mic", "system"):
        (base / f"{label}.wav").write_bytes(f"{label} audio".encode())
    for file, text in files.items():
        (base / file).write_text(text, encoding="utf-8")
    meta.set_started(base, STARTED)
    return base


def _inputs(base: Path, op: str) -> dict[str, Path]:
    if op == "recording_summary":
        return {"text": base / "transcript.txt"}
    return {label: base / f"{label}.wav" for label in ("mic", "system")}


def _state(base: Path) -> dict[str, str]:
    names = ("transcript.txt", "summary.md", "translation.md")
    return {
        name: (base / name).read_text(encoding="utf-8") for name in names if (base / name).exists()
    }


@pytest.mark.parametrize("request_", FIXTURE["requests"], ids=lambda r: r["key"])
def test_client_requests_fit_the_node_contract(request_):
    body = JobRequest.model_validate(request_)
    spec = ops.get(body.op)
    assert spec is not None
    assert (spec.input_kind == ops.InputKind.TRACKS) == (body.tracks is not None)
    tracks = {"tracks": request_["tracks"]} if body.tracks is not None else {}
    spec.params.model_validate({**body.params, **tracks})


@pytest.mark.parametrize("case", FIXTURE["jobs"], ids=lambda c: c["body"]["status"])
def test_node_answers_about_a_job_as_in_the_contract(tmp_path, case):
    app = create_app(tmp_path)
    job = app.state.store.create(op=case["op"], path=str(tmp_path), params={"name": "x"}, key="k")
    view = JobView.model_validate(case["body"])  # job progress as the node's queue reports it
    for field in JobView.model_fields.keys() - {"id"}:
        setattr(job, field, getattr(view, field))
    app.state.store.update(job)
    answer = TestClient(app).get(f"/api/jobs/{job.id}").json()
    assert answer == {**case["body"], "id": job.id}
    if answer["result"] is not None:
        result = ops.recording_result(case["op"]).model_validate(answer["result"])
        assert result.model_dump(mode="json") == answer["result"]


@pytest.mark.parametrize("case", FIXTURE["apply"], ids=lambda c: c["case"])
def test_result_is_applied_to_the_folder(tmp_path, case):
    for name in case.get("taken", ()):
        (tmp_path / name).mkdir()
    base = _folder(tmp_path, case["folder"], case["files"])
    node = Node(case["result"])
    params = {"name": case.get("sent", base.name), "started": STARTED}
    if case.get("refused"):
        with pytest.raises(MediaError):
            process(base, node.client, case["op"], params, _inputs(base, case["op"]))
        assert base.is_dir() and _state(base) == case["files"]
        assert not (tmp_path / "outside").exists()
        return
    out = process(base, node.client, case["op"], params, _inputs(base, case["op"]))
    after = case["after"]
    assert out.base == tmp_path / after["folder"]
    assert _state(out.base) == {k: v for k, v in after["files"].items() if v is not None}
    assert meta.last(out.base).job == "j1"


ANSWERS = FIXTURE["answers"]


def _answer(base: Path, node: Node, outcome: str) -> None:
    params = {"name": base.name, "started": STARTED}
    if outcome == "failed":
        with pytest.raises(MediaError):
            process(base, node.client, "recording", params, _inputs(base, "recording"))
    else:
        out = process(base, node.client, "recording", params, _inputs(base, "recording"))
        assert out.transcript_txt is not None


@pytest.mark.parametrize("case", ANSWERS["cases"], ids=lambda c: c["case"])
def test_client_answers_the_node_as_in_the_contract(tmp_path, case):
    base = _folder(tmp_path, "2026-10-23_12-30", {})
    if title := case.get("title"):
        set_own_title(base, title, create=True)
    node = Node(ANSWERS["done"], submit=case.get("submit", ()), poll=case.get("poll", ()))
    _answer(base, node, case["outcome"])
    assert node.uploads == case["uploads"]
    if title:
        assert own_title(base) == title
    if retry := case.get("retry"):
        node.submit, node.poll = list(retry.get("submit", ())), list(retry.get("poll", ()))
        _answer(base, node, retry["outcome"])
    first = list(dict.fromkeys(node.keys))
    assert [first.index(key) for key in node.keys] == case["keys"]


def test_name_taken_between_attempts_takes_the_next_free_one(tmp_path, monkeypatch):
    """The name was chosen, the apply was interrupted, and sync brought a folder with that name:
    the retry takes the next free name instead of failing every time."""
    from mnema.live import recording_client as rc

    base = _folder(tmp_path, "2026-10-23_12-30", {})
    node = Node(FIXTURE["apply"][0]["result"])
    params = {"name": base.name, "started": STARTED}
    write, failed = rc._atomic_bytes, []

    def full_disk(path, data):
        if path.name == "transcript.txt" and not failed:
            failed.append(path)
            raise OSError("disk full")
        write(path, data)

    monkeypatch.setattr(rc, "_atomic_bytes", full_disk)
    with pytest.raises(OSError):
        process(base, node.client, "recording", params, _inputs(base, "recording"))
    (tmp_path / "2026-10-23_Дейли").mkdir()
    out = process(base, node.client, "recording", params, _inputs(base, "recording"))
    assert out.base.name == "2026-10-23_Дейли (2)" and meta.last(out.base).job == "j1"
    assert len(node.keys) == 1


def test_key_of_another_device_job_is_refused_without_its_id(tmp_path, monkeypatch):
    """A new invitation is a new device: the server does not hand out the previous device's job
    key and names the reason for refusing."""
    monkeypatch.setenv("MNEMA_TOKEN", "node-token")
    app = create_app(tmp_path)
    app.state.operations["text"] = ops.OpSpec(
        name="text",
        fn=lambda path, params, on: {},
        params=ops.TranslateParams,
        input_kind=ops.InputKind.TEXT,
        output_kind=ops.OutputKind.JSON,
    )
    devices = DeviceStore(tmp_path / ".mnema" / "devices.db")
    tokens = []
    for name in ("Pixel", "Pixel после нового приглашения"):
        claimed = devices.claim(devices.create_invite(now=time.time())[0], name, now=time.time())
        assert claimed is not None
        tokens.append(claimed[1])
    client = TestClient(app)
    upload = client.post(
        "/api/upload",
        files={"file": ("text.txt", "текст".encode())},
        headers={"Authorization": f"Bearer {tokens[0]}"},
    ).json()["path"]
    request = {"op": "text", "key": "record-id-1", "path": upload}
    first = client.post("/api/jobs", json=request, headers={"Authorization": f"Bearer {tokens[0]}"})
    again = client.post("/api/jobs", json=request, headers={"Authorization": f"Bearer {tokens[0]}"})
    other = client.post("/api/jobs", json=request, headers={"Authorization": f"Bearer {tokens[1]}"})
    assert again.json() == first.json()
    assert other.status_code == 409 and other.json()["reason"] == "key_taken"
    assert first.json()["id"] not in other.text
