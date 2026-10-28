"""mnema-bar: one-click call recording from the macOS menu bar or the GNOME top bar.

Model and menu — state.py (pure, unit-tested on any OS); recording — runtime.py (a background
worker over live/setup.py in rec mode); GUI — the Swift Mnema.app with the headless.py engine, or
the app_linux.py tray (pystray); installing as an app — install.py. Heavy and platform imports are
lazy.
"""
