"""Юниты единого Settings: чтение файла настроек поверх окружения."""

from __future__ import annotations

from mnema.config import Settings
from mnema.llm.config import LLMSettings


def test_load_env_preserves_current_config_and_reads_model_lines_in_place(tmp_path, monkeypatch):
    import os

    import mnema.config as config

    env = config.user_env_file()
    env.parent.mkdir(parents=True)
    original = (
        "# current settings\nMNEMA_MODEL=openai/test\n"
        "MNEMA_API_KEY='key-$HOME'\nMNEMA_CA=/saved.pem\n"
    )
    env.write_text(original)
    monkeypatch.delenv("MNEMA_MODEL", raising=False)
    monkeypatch.delenv("MNEMA_API_KEY", raising=False)
    monkeypatch.setenv("MNEMA_CA", "/override.pem")
    project = tmp_path / "repo" / ".env"
    project.parent.mkdir()
    project.write_text("MNEMA_RUNNER=https://ignored.example\n")
    monkeypatch.setattr(config, "__file__", str(project.parent / "mnema" / "config.py"))

    config.load_env()
    config.load_env()

    assert LLMSettings().model == "openai/test"
    assert LLMSettings().api_key == "key-$HOME"
    assert "MNEMA_MODEL" not in os.environ and "MNEMA_API_KEY" not in os.environ
    assert Settings().ca == "/override.pem"
    assert not Settings().runner
    assert env.read_text() == original
    assert set(p.name for p in env.parent.iterdir()) == {".env"}
    env.write_text("MNEMA_MODEL=openai/edited\nMNEMA_API_KEY=key-2\n")
    assert (LLMSettings().model, LLMSettings().api_key) == ("openai/edited", "key-2")
