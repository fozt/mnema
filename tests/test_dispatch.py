"""dispatch: resolve_runner precedence and the local/remote paths of dispatch()."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel, ConfigDict


@pytest.mark.parametrize(
    "env, flag, local, expected",
    [
        (
            "http://env",
            "http://flag",
            False,
            "http://flag",
        ),  # an explicit flag beats the environment
        ("http://env", None, False, "http://env"),
        (None, None, False, None),  # local mode
        ("http://env", None, True, None),  # --local beats MNEMA_RUNNER
        ("http://env", "http://flag", True, None),  # and even an explicit --runner
    ],
)
def test_resolve_runner_explicit_wins_over_env(monkeypatch, env, flag, local, expected):
    from mnema.dispatch import resolve_runner

    if env:
        monkeypatch.setenv("MNEMA_RUNNER", env)
    else:
        monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    assert resolve_runner(flag, local=local) == expected


def test_dispatch_local_url_cleans_input_immediately(monkeypatch):
    from mnema import core, ops
    from mnema.dispatch import dispatch

    made = []

    def download(url, *, dest_dir, cookies_file, **kwargs):
        f = dest_dir / "clip.mp3"
        f.write_bytes(b"audio")
        made.append(f)
        assert cookies_file.read_text() == "cookie-secret"
        return f

    monkeypatch.setattr(core, "download_url", download)

    class NoParams(BaseModel):
        model_config = ConfigDict(extra="forbid")

    spec = ops.OpSpec(
        name="echo",
        fn=lambda p, kw, on: {"text": p.read_text()},
        params=NoParams,
        input_kind="media",
        output_kind="json",
    )
    monkeypatch.setitem(ops._REGISTRY, "echo", spec)
    result = dispatch(
        "echo", "https://example.com/clip", {}, runner=None, local=True, cookies="cookie-secret"
    )
    assert result == {"text": "audio", "source_name": "clip"}
    assert made and not made[0].parent.parent.exists()


def test_dispatch_remote_forwards_token(monkeypatch):
    """MNEMA_TOKEN reaches MnemaClient as token=."""
    from mnema.dispatch import dispatch

    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.setenv("MNEMA_TOKEN", "secret")

    mock_client = MagicMock()
    mock_client.upload.return_value = "/srv/a.mp3"
    mock_client.run.return_value = {"text": "ok"}

    with patch("mnema.client.MnemaClient", return_value=mock_client) as mock_cls:
        dispatch("transcribe", Path("/x/a.mp3"), {}, runner="http://srv")

    mock_cls.assert_called_once_with("http://srv", token="secret")


def test_dispatch_remote_extracts_audio_for_video(monkeypatch, tmp_path: Path):
    """An audio_only operation on a video: the client extracts the sound and uploads an mp3,
    not the whole video, and removes the ephemeral extraction folder after the job.
    """
    from mnema import dispatch as dispatch_mod
    from mnema.dispatch import dispatch

    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.delenv("MNEMA_TOKEN", raising=False)

    video = tmp_path / "call.mp4"
    video.write_bytes(b"fakevideo")

    mock_client = MagicMock()
    mock_client.upload.return_value = "/srv/call.mp3"
    mock_client.run.return_value = {"text": "ok"}

    fake_info = MagicMock()
    fake_info.has_video = True

    def fake_extract(src, dst, *, compressed, on):
        assert compressed is True  # a compressed mp3, not wav
        Path(dst).write_bytes(b"audio")
        return Path(dst)

    with (
        patch("mnema.client.MnemaClient", return_value=mock_client),
        patch.object(dispatch_mod.mc, "probe", return_value=fake_info),
        patch.object(dispatch_mod.mc, "extract_audio", side_effect=fake_extract) as mock_extract,
    ):
        result = dispatch("transcribe", video, {}, runner="http://srv")

    assert result == {"text": "ok"}
    mock_extract.assert_called_once()
    uploaded = Path(mock_client.upload.call_args.args[0])
    assert uploaded.suffix == ".mp3" and uploaded != video  # the sound is uploaded, not the video
    assert uploaded.parent.name.startswith("mnema-aud-")
    assert not uploaded.parent.exists()  # the extraction folder is gone


@pytest.mark.parametrize("op, name", [("transcribe", "voice.mp3"), ("compress", "clip.mp4")])
def test_dispatch_remote_uploads_audio_input_as_is(monkeypatch, tmp_path: Path, op, name):
    """An audio_only operation on audio, and an operation that needs the picture (compress) on
    a video: uploaded as is, without extracting the sound; compress does not even probe."""
    from mnema import dispatch as dispatch_mod
    from mnema.dispatch import dispatch

    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.delenv("MNEMA_TOKEN", raising=False)

    src = tmp_path / name
    src.write_bytes(b"media")

    mock_client = MagicMock()
    mock_client.upload.return_value = f"/srv/{name}"
    mock_client.run.return_value = {"text": "ok"}

    fake_info = MagicMock()
    fake_info.has_video = False

    with (
        patch("mnema.client.MnemaClient", return_value=mock_client),
        patch.object(dispatch_mod.mc, "probe", return_value=fake_info) as mock_probe,
        patch.object(dispatch_mod.mc, "extract_audio") as mock_extract,
    ):
        dispatch(op, src, {}, runner="http://srv")

    if op == "compress":
        mock_probe.assert_not_called()
    mock_extract.assert_not_called()
    assert mock_client.upload.call_args[0][0] == src and mock_client.upload.call_count == 1


def _cloud_completion(monkeypatch, answer="перевод"):
    import types

    import litellm

    calls = []

    def completion(**kw):
        calls.append(kw)
        choice = types.SimpleNamespace(
            delta=types.SimpleNamespace(content=answer), finish_reason="stop"
        )
        return iter([types.SimpleNamespace(choices=[choice])])

    monkeypatch.setattr(litellm, "completion", completion)
    return calls


def _real_client_factory(app):
    """A real MnemaClient over ASGITransport (the real server without network)."""
    import httpx

    from mnema.client import MnemaClient

    def factory(base_url, *, token=None):
        return MnemaClient(base_url, token=token, transport=httpx.ASGITransport(app=app))

    return factory


def _echo_capturing_spec(seen: list[str]):
    """A fake op: reads the input into seen (before the ephemeral cleanup) and echoes the path."""
    from pydantic import BaseModel

    from mnema import ops

    class _P(BaseModel):
        model_config = {"extra": "forbid"}

    def _fn(p, params, on):
        seen.append(Path(p).read_bytes().decode())  # read before uploads/<uuid>/ is removed
        return {"seen": str(p)}

    return ops.OpSpec(
        name="echo",
        fn=_fn,
        params=_P,
        input_kind=ops.InputKind.MEDIA,
        output_kind=ops.OutputKind.JSON,
    )


def test_e2e_uploads_are_unique_and_isolated(tmp_path: Path, monkeypatch):
    """E2E: files with one name and different content do not overwrite each other on upload.

    A unique uploads/<uuid>/ per upload; there is no dedup, each run uploads and processes its
    own.
    """
    from mnema.dispatch import dispatch
    from mnema.server import create_app

    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.delenv("MNEMA_TOKEN", raising=False)

    root = tmp_path / "root"
    root.mkdir()
    app = create_app(root)
    seen: list[str] = []
    app.state.operations = {"echo": _echo_capturing_spec(seen)}

    first = tmp_path / "a" / "talk.mp3"
    first.parent.mkdir()
    first.write_bytes(b"AAAAA")
    second = tmp_path / "b" / "talk.mp3"
    second.parent.mkdir()
    second.write_bytes(b"BBBBB")  # the same basename, other content

    with patch("mnema.client.MnemaClient", side_effect=_real_client_factory(app)):
        dispatch("echo", first, {}, runner="http://srv")
        dispatch("echo", second, {}, runner="http://srv")

    # Each run processed its own content: same-name uploads did not clash.
    assert seen == ["AAAAA", "BBBBB"]


def test_e2e_url_job_dispatches_via_server(tmp_path: Path, monkeypatch):
    """E2E: dispatch of a link with a runner: the client downloads it (mocked) into a temporary
    folder, uploads it, and the op sees the file; the folder is removed afterwards.
    """
    from mnema import core as mc
    from mnema.dispatch import dispatch
    from mnema.server import create_app

    monkeypatch.delenv("MNEMA_RUNNER", raising=False)
    monkeypatch.delenv("MNEMA_TOKEN", raising=False)

    root = tmp_path / "root"
    root.mkdir()
    app = create_app(root)
    seen: list[str] = []
    app.state.operations = {"echo": _echo_capturing_spec(seen)}

    dl_dirs: list[Path] = []

    def fake_download_url(url, *, want_video, dest_dir, cookies_file=None, on):
        d = Path(dest_dir)
        assert d.parent.name.startswith("mnema-input-")  # downloaded on the client
        assert not d.is_relative_to(root)
        dl_dirs.append(d)
        f = d / "clip.mp3"
        f.write_bytes(b"dl")
        return f

    monkeypatch.setattr(mc, "download_url", fake_download_url)

    with patch("mnema.client.MnemaClient", side_effect=_real_client_factory(app)):
        result = dispatch("echo", "https://example.com/clip", {}, runner="http://srv")

    assert Path(result["seen"]).name == "clip.mp3"
    assert seen == ["dl"]  # the op processed the downloaded file
    assert dl_dirs and not dl_dirs[0].exists()  # the temporary folder is removed


def test_dispatch_remote_upload_gets_own_progress_slice(tmp_path: Path):
    """The upload gets its own slice of the shared scale: its 100% is ~15% of the bar, not 100%
    (otherwise the bar sat at 100% while the runner's job was just starting)."""
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x")
    fractions: list[float] = []

    mock_client = MagicMock()

    def fake_upload(path, on=None):
        on(1.0, "Отправляю на сервер…")  # the upload is complete
        return "/srv/a.mp3"

    def fake_run(op, path, params, on=None):
        on(0.0, "старт задачи")  # the runner has just begun
        return {"ok": 1}

    mock_client.upload.side_effect = fake_upload
    mock_client.run.side_effect = fake_run

    from mnema.dispatch import dispatch as _dispatch

    with patch("mnema.client.MnemaClient", return_value=mock_client):
        _dispatch(
            "transcribe",
            src,
            {},
            runner="http://srv",
            on=lambda f, m: fractions.append(f) if f is not None else None,
        )

    assert fractions, "no progress arrived"
    assert max(fractions[:1]) <= 0.2  # the end of the upload is a small share, not 1.0
    assert all(f < 0.5 for f in fractions)  # the job start is early on the scale too
