"""The uv extras each installation ships: install.sh, the update and the Mac app build read them.

    python -I mnema/extras.py mac   # install.sh, before any environment exists

Standard library only: install.sh runs this file on a bare Python.
"""

import sys

# The Mac app (the DMG and ./install.sh client on a Mac): the panel's engine, mnema-mcp and the
# engines it ships; GigaAM stays on the server. No web stack: only `mnema serve`, a development
# command, needs it.
MAC = ("menubar", "mcp", "parakeet", "whisper")
LINUX_CLIENT = ("menubar", "mcp")  # recordings are processed by the server
SERVER = ("full", "live")

ROLES = {"mac": MAC, "linux": LINUX_CLIENT, "server": SERVER}

if __name__ == "__main__":
    print(" ".join(ROLES[sys.argv[1]]))
