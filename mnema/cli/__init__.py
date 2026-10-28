"""Rich CLI over mnema.core and mnema.llm: subcommands and terminal output."""

from __future__ import annotations

import os
import sys

from .. import core as mc
from ..dispatch import dispatch
from ..i18n import _
from .commands import cmd_transcribe, cmd_translate
from .parser import build_parser
from .sources import _DOWNLOADED, _out_path, _resolve_sources
from .ui import banner, console, die, run_streaming, run_with_progress


def main() -> None:
    from ..config import load_env
    from ..i18n import detect, set_language

    set_language(detect())  # before the banner and the parser help
    try:  # arrows, backspace and history in interactive prompts
        import readline  # noqa: F401
    except ImportError:
        pass

    banner()
    parser = build_parser()
    try:
        args = parser.parse_args()
        load_env()
        if not args.cmd:
            parser.print_help()
            return
        args.func(args)
    except mc.MediaError as exc:
        die(str(exc))
    except (KeyboardInterrupt, EOFError):
        # Cancel the active remote job, otherwise it keeps the runner busy with nobody waiting.
        from ..client import cancel_active_job

        cancel_active_job()
        console.print(f"\n[yellow]{_('Exiting.')}[/]")
        # os._exit, not sys.exit: litellm and WhisperLiveKit leave thread pools that atexit
        # would join forever. Everything needed is already saved and printed.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)


__all__ = [
    "_DOWNLOADED",
    "_out_path",
    "_resolve_sources",
    "build_parser",
    "cmd_transcribe",
    "cmd_translate",
    "console",
    "dispatch",
    "main",
    "mc",
    "run_streaming",
    "run_with_progress",
]
