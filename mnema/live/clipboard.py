"""System clipboard for dictation (macOS, Linux; best effort)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the clipboard. False without a graphical session or tool, or on failure.
    Linux: Wayland via wl-copy, X11 via xclip/xsel; other users' displays are not searched."""
    if not text.strip():
        return False
    commands: list[list[str]] = []
    if sys.platform == "darwin":
        commands = [["pbcopy"]]
    elif sys.platform == "linux":
        if os.environ.get("WAYLAND_DISPLAY"):
            commands.append(["wl-copy"])
        if os.environ.get("DISPLAY"):
            commands += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
        commands = [cmd for cmd in commands if shutil.which(cmd[0])]
    for command in commands:
        try:
            subprocess.run(
                command, input=text.encode("utf-8"), check=True, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )  # fmt: skip
            return True
        except (OSError, subprocess.SubprocessError):
            logger.debug("%s failed, trying another clipboard tool", command[0], exc_info=True)
    return False
