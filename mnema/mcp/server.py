"""mnema MCP server (stdio transport): the @op registry operations exposed as MCP tools.

A sibling of the CLI, not a layer on top of it: the tools are thin wrappers over tools.py
(→ dispatch) and process where the app does (a Mac by itself or on the server, as chosen in the
app; other systems on the server); settings come from ~/.config/mnema/.env, as for the CLI. The
MCP SDK is imported lazily in _build, so the module imports without `mcp` installed (thin client,
CI). Entry point: `mnema-mcp`.
"""

from __future__ import annotations

from typing import Any

from ..i18n import _
from . import tools


def _build() -> Any:
    """Builds the FastMCP server with the mnema tools (the SDK is imported here, lazily)."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise SystemExit(_("The MCP SDK is missing — reinstall Mnema (./install.sh client)")) from e

    mcp = FastMCP("mnema")

    @mcp.tool()
    def transcribe(source: str, lang: str | None = None) -> dict:
        """Transcribe a recording (a local path or a URL) to text.

        Returns transcript_path (the transcript saved to disk), text and language.
        """
        return tools.transcribe_recording(source, lang=lang)

    @mcp.tool()
    def translate(source: str, to_lang: str = "ru") -> dict:
        """Transcribe and translate a recording to markdown (needs an LLM model)."""
        return tools.translate_recording(source, to_lang=to_lang)

    @mcp.tool()
    def list_recordings() -> list[dict]:
        """List the recordings in this computer's library: name, path, files."""
        return tools.list_recordings()

    @mcp.tool()
    def read_transcript(path: str) -> str:
        """Read a saved transcript by the path of its file or of the recording folder."""
        return tools.read_transcript(path)

    return mcp


def main() -> None:
    """mnema-mcp entry point: the app settings, then the stdio server."""
    from ..config import load_env

    load_env()
    _build().run()


if __name__ == "__main__":
    main()
