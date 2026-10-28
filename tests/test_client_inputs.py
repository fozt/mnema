"""URLs and cookies stay on the client: external yt-dlp and ffmpeg, the real HTTP contract."""

import json
import shlex
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yt_dlp
from pydantic import BaseModel, ConfigDict

from mnema import ops
from mnema.client import MnemaClient
from mnema.core.base import MediaError
from mnema.inputs import local_input
from mnema.server import create_app


@pytest.fixture
def downloads(monkeypatch):
    calls = []
    videos = set()
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )

    def extract(self, url, download=True):
        folder = Path(self.params["outtmpl"]).parent
        suffix = (
            ".webm"
            if url.endswith("mixed")
            else (".m4a" if self.params["format"] == "ba/b" else ".mp4")
        )
        if url.endswith("fallback"):
            suffix = ".mp4"
        path = folder / ("Same title" + suffix)
        cookie = self.params.get("cookiefile")
        if cookie:
            cookie = Path(cookie)
            assert cookie.stat().st_mode & 0o777 == 0o600
            assert cookie.read_text() == "private-cookie"
        calls.append((url, path, cookie, self.params["format"]))
        if url.endswith("fallback"):
            videos.add(path)
        if url.endswith("fail"):
            path.write_bytes(b"partial")
            raise RuntimeError("download failed")
        for hook in self.params["progress_hooks"]:
            hook({"status": "downloading", "total_bytes": 4, "downloaded_bytes": 4})
        path.write_bytes(str(len(calls)).encode())
        return {"requested_downloads": [{"filepath": str(path)}]}

    class Downloader:
        extract_info = extract

        def __init__(self, params):
            self.params = params

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Downloader)

    def process(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            field = cmd[cmd.index("-show_entries") + 1]
            if field == "format=duration":
                value = str(len(Path(cmd[-1]).read_bytes()))
            elif field == "stream=codec_name":
                value = (
                    "aac"
                    if cmd[cmd.index("-select_streams") + 1] == "a"
                    else ("h264" if Path(cmd[-1]) in videos else "")
                )
            elif field == "stream=width,height":
                value = "32x32"
            elif field == "stream=r_frame_rate":
                value = "1/1"
            else:
                raise AssertionError(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=value, stderr="")
        if cmd[0] == "ffmpeg" and "-vn" in cmd:
            Path(cmd[-1]).write_bytes(Path(cmd[cmd.index("-i") + 1]).read_bytes())
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        assert cmd[0] == "ffmpeg" and cmd[cmd.index("-f") + 1] == "concat"
        listing = Path(cmd[cmd.index("-i") + 1]).read_text().splitlines()
        paths = [Path(shlex.split(line)[1]) for line in listing]
        Path(cmd[-1]).write_bytes(b"".join(p.read_bytes() for p in paths))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    popen = subprocess.Popen

    def start(cmd, **kwargs):
        result = process(cmd)
        # The Popen path is needed for a cancellable ffmpeg: keep the real pipe/wait while the
        # external stub still processes the synthetic media.
        return popen(
            [sys.executable, "-c", "import sys; print(sys.argv[1], end='')", result.stdout],
            **kwargs,
        )

    monkeypatch.setattr(subprocess, "run", process)
    monkeypatch.setattr(subprocess, "Popen", start)
    return calls


@pytest.mark.parametrize("as_string", [False, True])
def test_local_input_never_removes_existing_file(tmp_path, as_string):
    path = tmp_path / "local.mp3"
    path.write_bytes(b"user media")
    with pytest.raises(RuntimeError), local_input(str(path) if as_string else path) as prepared:
        assert prepared == path
        raise RuntimeError("operation failed")
    assert path.read_bytes() == b"user media"


@pytest.mark.parametrize("suffix", ["second", "mixed"])
def test_local_input_merges_in_order_and_cleans_cookies(downloads, suffix):
    progress = []
    with local_input(
        ["https://example.com/first", f"https://example.com/{suffix}"],
        cookies="private-cookie",
        on=lambda f, m: progress.append(f),
    ) as path:
        assert path.name == "merged.mp4" and path.read_bytes() == b"12"
        assert downloads[0][1].parent != downloads[1][1].parent
    assert all(not item[1].exists() and not item[2].exists() for item in downloads)
    assert not path.parent.exists()
    numbers = [f for f in progress if f is not None]
    assert numbers == sorted(numbers)


@pytest.mark.parametrize("exit_kind", ["download", "operation", "cancel"])
def test_local_input_cleans_partial_files_on_failure_or_cancel(downloads, exit_kind):
    link = "https://example.com/fail" if exit_kind == "download" else "https://example.com/a"

    def on(f, message):
        if exit_kind == "cancel" and f is not None:
            raise KeyboardInterrupt

    error = KeyboardInterrupt if exit_kind == "cancel" else MediaError
    with pytest.raises(error), local_input(link, cookies="private-cookie", on=on) as path:
        assert path.is_file()
        raise MediaError("operation failed")
    assert downloads
    assert all(not item[1].parent.exists() and not item[2].exists() for item in downloads)


@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize("audio_only", [False, True])
def test_client_uploads_downloaded_input_and_sends_no_urls_or_cookies(
    tmp_path, downloads, multiple, audio_only, monkeypatch
):
    payloads, progress = [], []
    app = create_app(tmp_path)

    def operation(path, params, on):
        assert path.is_relative_to(tmp_path / "uploads")
        assert all(not item[1].exists() and not item[2].exists() for item in downloads)
        content = path.read_bytes().decode()
        on(0, "Начало операции")
        on(1, "Готово")
        return {"text": content}

    app.state.operations = {
        "fake": ops.OpSpec(
            name="fake",
            fn=operation,
            params=ops.TranscribeParams,
            input_kind=ops.InputKind.MEDIA,
            output_kind=ops.OutputKind.TEXT,
            audio_only=audio_only,
        )
    }

    monkeypatch.setitem(ops._REGISTRY, "fake", app.state.operations["fake"])

    class RecordingTransport(httpx.ASGITransport):
        async def handle_async_request(self, request):
            if request.url.path == "/api/jobs":
                payloads.append(json.loads(await request.aread()))
            return await super().handle_async_request(request)

    client = MnemaClient("http://test", transport=RecordingTransport(app=app))
    source = (
        {"urls": ["https://example.com/a", "https://example.com/b"]}
        if multiple
        else {"url": "https://example.com/a"}
    )
    result = client.run(
        "fake",
        **source,
        cookies="private-cookie",
        on=lambda f, m: progress.append(f),
        poll_interval=0.001,
    )
    assert result == {
        "text": "12" if multiple else "1",
        "source_name": "merged" if multiple else "Same title",
    }
    assert all(item[3] == ("ba/b" if audio_only else "bv*+ba/b") for item in downloads)
    assert len(payloads) == 1 and set(payloads[0]) == {"op", "path", "params"}
    assert "private-cookie" not in json.dumps(payloads)
    assert not Path(payloads[0]["path"]).parent.exists()
    numbers = [f for f in progress if f is not None]
    assert numbers == sorted(numbers)


def test_download_failure_never_creates_server_job(downloads):
    requests = []
    client = MnemaClient("http://test", transport=httpx.MockTransport(lambda r: requests.append(r)))
    with pytest.raises(MediaError, match="download failed"):
        client.run("transcribe", url="https://example.com/fail", cookies="private-cookie")
    assert requests == []
    assert all(not item[1].parent.exists() and not item[2].exists() for item in downloads)


@pytest.mark.parametrize("tail", ["mixed", "fallback"])
def test_audio_inputs_merge_audio_even_when_site_returns_video(downloads, tail):
    with local_input(
        ["https://example.com/a", f"https://example.com/{tail}"], want_video=False
    ) as path:
        assert path.suffix == ".m4a" and path.read_bytes() == b"12"
        assert all(item[3] == "ba/b" for item in downloads)
        if tail == "fallback":
            assert (downloads[-1][1].parent / "audio" / "Same title.mp3").is_file()
    assert not path.parent.exists()


@pytest.mark.parametrize("requires_model", [False, True])
def test_dispatch_audio_url_selects_audio_format_for_local_and_cloud_steps(
    monkeypatch, downloads, requires_model
):
    from mnema.dispatch import dispatch

    class NoParams(BaseModel):
        model_config = ConfigDict(extra="forbid")

    spec = ops.OpSpec(
        name="fixture-audio",
        fn=lambda path, params, on: {"text": path.read_text()},
        params=NoParams,
        input_kind=ops.InputKind.MEDIA,
        output_kind=ops.OutputKind.TEXT,
        audio_only=True,
        requires_model=requires_model,
    )
    monkeypatch.setitem(ops._REGISTRY, spec.name, spec)
    result = dispatch(spec.name, "https://example.com/a", {}, runner=None, local=True)
    assert result == {"text": "1", "source_name": "Same title"}
    assert downloads[0][3] == "ba/b"
    assert not downloads[0][1].exists()
