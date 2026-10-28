"""Plain logic of the mnema MCP tools: wrappers over dispatch. It does NOT import the MCP SDK, so
it imports and is unit-tested on any platform (CI without engines). FastMCP is built in server.py.

A tool calls an op through dispatch() where the app and the CLI process (node.selected_processing):
a Mac by itself or on the server, as chosen in the app; other systems on the server. URLs, files
and uploads to the server are handled inside dispatch.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

from ..connection import Connection
from ..dispatch import dispatch
from ..i18n import _


def _transcripts_root() -> Path:
    """Raw MCP transcripts live outside the recording library, so sync never publishes them."""
    from ..config import data_dir

    return data_dir() / "transcripts"


def _node() -> Connection | None:
    """Where to process (node.selected_processing); None is this computer."""
    from ..node import selected_processing

    return selected_processing()


def _slug(source: str) -> str:
    stem = Path(source).stem or "transcript"
    s = re.sub(r"[^\w-]+", "-", stem.strip().lower(), flags=re.UNICODE).strip("-")
    return s[:60] or "transcript"


def transcribe_recording(source: str, *, lang: str | None = None) -> dict:
    """Transcribe a recording (a local path OR a URL) to text.

    The raw transcript is saved to disk (outside the recording library) and its path returned, so
    the agent's summary can point at the source instead of holding all of it in memory.
    """
    node = _node()
    result = dispatch("transcribe", source, {"lang": lang}, runner=node, local=node is None)
    text = str(result.get("text") or "").strip()
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = _transcripts_root() / f"{_slug(source)}-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "transcript.txt"
    path.write_text(text, encoding="utf-8")
    return {"transcript_path": str(path), "language": result.get("language"), "text": text}


def translate_recording(source: str, *, to_lang: str = "ru") -> dict:
    """Transcribe and translate a recording with the server's model. Returns the markdown."""
    node = _node()
    result = dispatch("translate", source, {"to_lang": to_lang}, runner=node, local=node is None)
    return {"markdown": result.get("markdown", "")}


def list_recordings() -> list[dict]:
    """Recordings of this computer's library, by the same walk as the app feed, whatever the
    processing server: with a library server, sync keeps the local copy, and read_transcript reads
    files by the paths in the answer."""
    from ..config import library_root
    from ..library.walk import recordings
    from ..sync.state import known_sections

    return recordings(library_root(), known=known_sections())


def read_transcript(path: str) -> str:
    """Read a saved transcript (the local path of the file or of the recording folder)."""
    p = Path(path)
    if p.is_dir():
        p = p / "transcript.txt"
    if not p.is_file():
        raise FileNotFoundError(_("No such file: {path}", path=p))
    return p.read_text(encoding="utf-8")
