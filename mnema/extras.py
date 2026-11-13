"""The uv extras each installation ships: install.sh, the update and the Mac app build read them.

    python -I mnema/extras.py mac   # install.sh, before any environment exists

Standard library only: install.sh runs this file on a bare Python.
"""

import sys

# The Mac app (the DMG): the panel's engine only. It recognizes through mnema-asr, the Swift
# helper in the bundle; the command is a separate package (the "cli" extra).
MAC = ("menubar",)
LINUX_CLIENT = ("menubar", "cli")  # recordings are processed by the server
SERVER = ("full",)

ROLES = {"mac": MAC, "linux": LINUX_CLIENT, "server": SERVER}

if __name__ == "__main__":
    print(" ".join(ROLES[sys.argv[1]]))
