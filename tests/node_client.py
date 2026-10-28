"""The client/node boundary in tests: the real API without network connections."""

import shutil
from pathlib import Path

import httpx


def connect(monkeypatch):
    from mnema import config, node_settings
    from mnema.client import MnemaClient
    from mnema.core import audio
    from mnema.server import create_app

    app = create_app(config.user_env_file().parent / "test-node")
    monkeypatch.setattr(MnemaClient, "health", lambda self, **kw: {"ok": True})
    monkeypatch.setattr(
        MnemaClient,
        "_make_async_client",
        lambda self: httpx.AsyncClient(
            base_url=self._base_url, headers=self._headers, transport=httpx.ASGITransport(app=app)
        ),
    )
    monkeypatch.setattr(node_settings, "asr_problem", lambda *a, **k: None)
    monkeypatch.setattr(
        audio, "extract_audio", lambda src, dst, **kw: Path(shutil.copyfile(src, dst))
    )
    return app
