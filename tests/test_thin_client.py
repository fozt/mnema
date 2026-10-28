"""The thin client: importing mnema.ops and mnema.cli pulls in no heavy engines.

A subprocess with an import hook that blocks heavy packages checks a cold import of
mnema.ops and mnema.cli (the thin client's real entry point).
"""

from __future__ import annotations

import subprocess
import sys

# Heavy and platform packages the thin client must never load: transcription engines, LLM
# SDKs, the runner's web framework, yt-dlp and its cookies, diarization.
_BLOCKED = {
    "mlx_whisper",
    "whisperx",
    "torch",
    "litellm",
    "fastapi",
    "yt_dlp",
    "secretstorage",
    "deno",
    "pyannote",
    "openai",
}

_BLOCKER_PRELUDE = (
    "import sys\n"
    f"blocked = {_BLOCKED!r}\n"
    "class Blocker:\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name.split('.')[0] in blocked:\n"
    "            raise ImportError('blocked by the thin client test: ' + name)\n"
    "        return None\n"
    "sys.meta_path.insert(0, Blocker())\n"
)


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)


def test_import_ops_and_cli_without_engines() -> None:
    """mnema.ops and mnema.cli import without engines, the op registry is complete and
    nothing leaks.

    A missing `@op` import would silently drop an operation, and ty/ruff would not notice.
    """
    script = (
        _BLOCKER_PRELUDE
        + "import mnema.ops as ops\n"
        + "import mnema.cli\n"
        + "names = set(ops.registry())\n"
        + "expected = {'transcribe', 'translate', 'compress', 'mp3'}\n"
        + "missing = expected - names\n"
        + "assert not missing, 'missing operations: ' + repr(missing)\n"
        + "leaked = [m for m in blocked if sys.modules.get(m) is not None]\n"
        + "assert not leaked, 'engines leaked: ' + repr(leaked)\n"
        + "print('OK')\n"
    )
    proc = _run(script)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.startswith("OK")


def test_serve_without_web_stack_stops_with_a_message(tmp_path) -> None:
    """The Mac app ships no fastapi: `mnema serve` says what is missing, no traceback."""
    script = (
        _BLOCKER_PRELUDE
        + "import mnema.cli\n"
        + f"sys.argv = ['mnema', 'serve', '--port', '18765', '--dir', {str(tmp_path)!r}]\n"
        + "mnema.cli.main()\n"
    )
    proc = _run(script)
    assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "Traceback" not in proc.stderr
    assert "extra full" in proc.stdout
    assert not (tmp_path / ".mnema").exists()
