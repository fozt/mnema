"""The recording library lives only on an installed server.

Boundaries: a real `mnema serve` process (the server the installer starts), no systemd.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

from mnema.install_setup import configure_server, server_identity

REPO = Path(__file__).resolve().parents[1]
START_S = 60.0  # a real `mnema serve` under xdist load does not come up instantly


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _get(port: int, token: str, path: str) -> httpx.Response | None:
    try:
        return httpx.get(
            f"http://127.0.0.1:{port}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=2.0,
            trust_env=False,
        )
    except httpx.HTTPError:
        return None


def test_server_set_up_by_the_installer_serves_the_library(home, monkeypatch):
    """`./install.sh server` without systemd: `run-server` starts the service with the server.env
    environment and serves the library; the shared ~/.config/mnema/.env does not get the flag."""
    pytest.importorskip("uvicorn")
    monkeypatch.delenv("MNEMA_LIBRARY", raising=False)
    repo = home / "repo"
    (repo / ".venv/bin").mkdir(parents=True)
    launcher = repo / ".venv/bin/mnema"  # the console script uv installs
    launcher.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -c "from mnema.cli import main; main()" "$@"\n'
    )
    launcher.chmod(0o755)
    configure_server(repo, systemd=None, user="mnema")
    config = home / ".config/mnema"
    assert dotenv_values(config / "server.env")["MNEMA_LIBRARY"] == "true"
    assert "MNEMA_LIBRARY" not in dotenv_values(config / ".env")
    port, token = server_identity()
    assert dotenv_values(config / "server.env")["MNEMA_LIBRARY"] == "true"
    env = {k: v for k, v in os.environ.items() if k not in ("MNEMA_TOKEN", "MNEMA_LIBRARY")}
    env["PYTHONPATH"] = str(REPO)
    server = subprocess.Popen(
        [sys.executable, "-m", "mnema.install_setup", "run-server", "--repo", str(repo)],
        env=env, cwd=home, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + START_S
        while (library := _get(port, token, "/api/library")) is None:
            assert server.poll() is None, "the server did not start"
            assert time.monotonic() < deadline, "the server did not answer"
            time.sleep(0.1)
        assert library.status_code == 200
        assert library.json()["records"] == []
        assert _get(port, "wrong", "/api/library").status_code == 401
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(server.pid, signal.SIGKILL)
            server.wait(timeout=10)
