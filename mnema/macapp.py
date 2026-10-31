"""Mnema.app: its bundle identifier and where its recognition helper is.

Standard library only: scripts/build_mac_app.py and scripts/smoke_mac_app.py run on a bare Python.
"""

from pathlib import Path

APP_ID = "com.mnema.bar"  # CFBundleIdentifier of Mnema.app
HELPER = "Contents/MacOS/mnema-asr"  # recognition on CoreML (core/mac_helper.py)


def app_helper(bundle: Path) -> Path:
    """mnema-asr of a Mnema.app."""
    return bundle / HELPER
