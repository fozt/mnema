"""Dictation clipboard (pbcopy is the external boundary, mocked)."""

from __future__ import annotations

import subprocess

import pytest

from mnema.live import clipboard


def test_copy_to_clipboard_empty_text_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    monkeypatch.setattr(
        clipboard.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    assert clipboard.copy_to_clipboard("   ") is False  # pbcopy was not called


def test_copy_to_clipboard_failure_returns_false(monkeypatch) -> None:
    """A pbcopy failure is False, not an exception: the clipboard is a convenience."""

    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "pbcopy")

    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    monkeypatch.setattr(clipboard.subprocess, "run", boom)
    assert clipboard.copy_to_clipboard("текст") is False


def test_copy_to_clipboard_headless_linux_is_false(monkeypatch) -> None:
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(clipboard.subprocess, "run", lambda *a, **k: pytest.fail("headless"))
    assert clipboard.copy_to_clipboard("текст") is False


@pytest.mark.parametrize(
    ("platform", "wayland", "display", "installed", "expected"),
    [
        ("darwin", "", "", set(), ["pbcopy"]),
        ("linux", "wayland-1", ":0", {"wl-copy", "xclip"}, ["wl-copy"]),
        ("linux", "", ":0", {"xclip"}, ["xclip", "-selection", "clipboard"]),
        ("linux", "", ":0", {"xsel"}, ["xsel", "--clipboard", "--input"]),
    ],
)
def test_linux_clipboard_uses_active_session(
    monkeypatch, platform, wayland, display, installed, expected
):
    monkeypatch.setattr(clipboard.sys, "platform", platform)
    monkeypatch.setenv("WAYLAND_DISPLAY", wayland)
    monkeypatch.setenv("DISPLAY", display)
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: name if name in installed else None)
    calls = []
    monkeypatch.setattr(clipboard.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    assert clipboard.copy_to_clipboard("русский текст")
    assert len(calls) == 1 and calls[0][0] == expected
    assert calls[0][1]["input"] == "русский текст".encode()
    assert calls[0][1]["timeout"] == 5 and calls[0][1]["check"]


def test_linux_clipboard_timeout_tries_x11_fallback(monkeypatch):
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: name)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[0])
        if cmd[0] == "wl-copy":
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(clipboard.subprocess, "run", run)
    assert clipboard.copy_to_clipboard("текст") and calls == ["wl-copy", "xclip"]
