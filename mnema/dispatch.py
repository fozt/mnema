"""One entry point for running an operation: locally (the ops registry) or remotely (MnemaClient).

The node does ASR and cloud model work; the client sends media and parameters. The runner is
chosen by node.selected_processing: --local, --runner, then a Mac processes itself or on the
server as chosen in the app, everything else uses MNEMA_RUNNER.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path
from typing import Any

from . import core as mc
from . import ops
from .connection import Connection
from .core.base import MediaError, Progress, noop, phased
from .i18n import _

# Ready text artifacts (a transcript, a translation): the op reads them as text (_transcript_text)
_TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".json")


def resolve_runner(explicit: str | Connection | None, *, local: bool = False) -> str | None:
    """The address of the processing server (node.selected_processing), or None for this
    computer; local=True means this computer whatever is chosen."""
    from .node import selected_processing

    remote = selected_processing(explicit, local=local)
    return remote.url if remote else None


def op_runner(
    op: str, explicit: str | Connection | None, *, local: bool = False
) -> Connection | None:
    """Where op runs, None for this computer: where processing runs (node.selected_processing),
    and an operation that needs the model (requires_model) on a Mac that recognizes here goes to
    the connected server with the user's model (node.summary_server); local=True is here."""
    from .node import selected_processing, summary_server

    if local:
        return None
    spec = ops.get(op)
    remote = selected_processing(explicit)
    return remote or (summary_server() if spec is not None and spec.requires_model else None)


def dispatch(
    op: str,
    src: Path | str | list[str],
    params: dict[str, Any],
    *,
    runner: str | Connection | None,
    local: bool = False,
    cookies: str | None = None,
    on: Progress = noop,
) -> dict:
    """Runs op on src (a local path or a link) and returns the result dict.

    The client handles links and cookies; the node runs the operation with its own models.
    """
    base_url = resolve_runner(runner, local=local)
    spec = ops.get(op)
    if base_url is None:
        from .inputs import local_input

        if spec is None:
            raise MediaError(_("Unknown operation: {op}", op=op))
        validated = spec.params.model_validate(params).model_dump(exclude_none=True)
        downloaded = isinstance(src, list) or mc.is_url(str(src))
        p_input, p_op = phased(on, 0.15, 0.85) if downloaded else (noop, on)
        with local_input(src, cookies=cookies, want_video=not spec.audio_only, on=p_input) as path:
            result = spec.fn(path, validated, p_op)
            if downloaded:
                result.setdefault("source_name", path.stem)
                if spec.output_kind == ops.OutputKind.FILE and result.get("path"):
                    # The file must outlive the input cleanup until fetch_result_file.
                    output = Path(tempfile.mkdtemp(prefix="mnema-output-"))
                    atexit.register(shutil.rmtree, output, ignore_errors=True)
                    saved = output / Path(result["path"]).name
                    shutil.move(result["path"], saved)
                    result["path"] = str(saved)
            return result
    if isinstance(src, list):
        from .client import MnemaClient
        from .config import Settings

        client = MnemaClient(
            base_url,
            token=runner.token if isinstance(runner, Connection) else Settings().token or None,
        )
        return client.run(op, params=params, urls=src, cookies=cookies, on=on)
    is_url = mc.is_url(str(src))

    from .client import MnemaClient
    from .config import Settings

    client = MnemaClient(
        base_url, token=runner.token if isinstance(runner, Connection) else Settings().token or None
    )
    if is_url:
        # The client downloads the link and uploads the file; cookies stay here.
        return client.run(op, params=params, url=str(src), cookies=cookies, on=on)
    # A local file. For audio operations (transcribe, translate) on a video, extract the sound
    # here (ffmpeg only, no engines) and upload a small mp3 instead of the whole video.
    # Operations that need the picture (compress) and text sidecars upload the file as is.
    upload_src = Path(src)
    tmp_dir: str | None = None
    extract = False
    if spec is not None and spec.audio_only and Path(src).suffix.lower() not in _TEXT_SUFFIXES:
        # text files skip probe: ffprobe opens a long .txt as "tty" video (has_video=True)
        # and extracting its "audio" failed the op
        try:
            extract = mc.probe(Path(src)).has_video
        except MediaError:
            extract = False  # not media: upload as is
    # One scale for all phases: extraction and upload get their own slices, not all of 0..1,
    # or the bar would show 100% after the upload while the runner's job has just begun.
    if extract:
        p_extract, p_upload, p_job = phased(on, 0.1, 0.1, 0.8)
    else:
        p_extract, (p_upload, p_job) = noop, phased(on, 0.15, 0.85)
    try:
        if extract:
            tmp_dir = tempfile.mkdtemp(prefix="mnema-aud-")
            upload_src = mc.extract_audio(
                Path(src), Path(tmp_dir) / (Path(src).stem + ".mp3"), compressed=True, on=p_extract
            )
        # ephemeral: the runner deletes the file after the job
        server_path = client.upload(upload_src, on=p_upload)
        return client.run(op, server_path, params, on=p_job)
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def fetch_result_file(
    result_path: str, dest: Path, *, runner: str | Connection | None, local: bool = False
) -> Path:
    """Fetches an op's file result (compress/mp3) to dest, the counterpart of dispatch().

    Locally the op writes next to its input, so a path other than dest is moved (-o given, or
    the input was downloaded to a temporary folder). Remotely the path is on the runner's disk
    (outputs/<job-id>/) and is downloaded with GET /api/download.
    """
    if not result_path:
        raise MediaError(_("The operation did not return the result file path"))
    base_url = resolve_runner(runner, local=local)
    if base_url is None:
        p = Path(result_path)
        if p.resolve() != dest.resolve():
            shutil.move(str(p), str(dest))
        return dest
    from .client import MnemaClient
    from .config import Settings

    client = MnemaClient(
        base_url, token=runner.token if isinstance(runner, Connection) else Settings().token or None
    )
    return client.download(result_path, dest)
