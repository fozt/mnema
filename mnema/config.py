"""Typed configuration from the environment (pydantic-settings).

One source of truth for every mnema environment variable: names, types, defaults and aliases.
Modules read Settings(), not the raw environment.

Model settings are read by LLMSettings on every creation. The provider key is explicit
(MNEMA_API_KEY): without it there is a clear error, no provider-specific key is picked up.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from .core.settings import FILE_SETTINGS, RuntimeSettings, read_settings_file
from .llm.config import MODEL_VARS


def config_dir() -> Path:
    """Mnema settings directory: $XDG_CONFIG_HOME/mnema, else ~/.config/mnema. Every settings file
    (.env, bar.json, sync.json, node.json, server.json, server.env) lives there."""
    import os

    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "mnema"


def data_dir() -> Path:
    """Mnema data outside the recording library: $XDG_DATA_HOME/mnema, else ~/.local/share/mnema."""
    import os

    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "mnema"


def bar_prefs_file() -> Path:
    """App settings (bar.json): written by the app; the CLI and MCP also read where a Mac
    processes (node.selected_processing)."""
    return config_dir() / "bar.json"


def user_env_file() -> Path:
    """Personal settings outside the repository (server, token, model): the .env file."""
    return config_dir() / ".env"


def library_root(default: Path | None = None) -> Path:
    """Recording library root: MNEMA_LIVE_DIR, else default (CLI: recordings/ in the working
    directory), else ~/mnema. Never created here: a missing directory is not an empty library."""
    configured = Settings().live_dir
    root = Path(configured).expanduser() if configured else default or Path.home() / "mnema"
    return root.absolute()


_FROM_FILE: dict[str, str] = {}  # name -> value copied from .env into the environment
_PER_USE = {*MODEL_VARS, *FILE_SETTINGS}  # read from the file on every use


def load_env() -> None:
    """Copy ~/.config/mnema/.env into the process environment; existing variables win. Model lines
    (llm.config.MODEL_VARS) and core.settings.FILE_SETTINGS (CPU threads) are not copied: they are
    read from the file on every use, so an edit is seen without a restart. An unreadable file
    (another owner, not UTF-8) logs a warning and the command keeps working."""
    import logging
    import os

    from dotenv import dotenv_values

    from .llm.redact import install_redaction

    install_redaction()
    read_settings_file(lambda: user_env_file())
    path = user_env_file()
    try:
        values = dotenv_values(path, interpolate=False) if path.is_file() else {}
    except (OSError, UnicodeError) as e:
        logging.getLogger("mnema").warning("cannot read %s: %s", path, e)
        return
    for name, value in values.items():
        if value is not None and name not in _PER_USE and name not in os.environ:
            os.environ[name] = _FROM_FILE[name] = value


def process_env(name: str) -> str | None:
    """A process environment variable not set from ~/.config/mnema/.env (service, export,
    parent); None if it came from the file or is unset."""
    import os

    value = os.environ.get(name)
    return None if value is None or _FROM_FILE.get(name) == value else value


def set_env(values: dict[str, str | None]) -> list[str]:
    """The only writer of ~/.config/mnema/.env: set the given lines (None removes one) in one
    atomic replace, other lines intact, file 600, directory 700. Everything except the model lines
    (LLMSettings reads those from the file) also goes into the process environment. Returns the
    removed names."""
    import os
    import shutil
    import tempfile

    from dotenv import dotenv_values, set_key, unset_key

    path = user_env_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    os.close(fd)
    draft = Path(temporary)
    try:
        if path.is_file():
            shutil.copyfile(path, draft)
        present = dotenv_values(draft, interpolate=False)
        removed = [n for n, v in values.items() if v is None and n in present]
        for name in removed:
            unset_key(draft, name)
        for name, value in values.items():
            if value is not None:
                set_key(draft, name, value, quote_mode="always")
        draft.chmod(0o600)
        os.replace(draft, path)
    finally:
        draft.unlink(missing_ok=True)
    for name, value in values.items():
        if name in _PER_USE:
            continue
        if value is None:
            if name in _FROM_FILE and process_env(name) is None:
                os.environ.pop(name, None)
            _FROM_FILE.pop(name, None)
        else:
            os.environ[name] = _FROM_FILE[name] = value
    return removed


class Settings(RuntimeSettings):
    """Every mnema environment setting. Fields are optional so Settings() works in any context;
    callers check the fields they require (the LLMSettings validator and others)."""

    model_config = SettingsConfigDict(extra="ignore")

    update_url: str = Field(
        default="https://github.com/fozt/mnema/releases/latest/download/mnema-release.json",
        validation_alias="MNEMA_UPDATE_URL",
    )

    # runner / client
    token: str | None = Field(default=None, validation_alias="MNEMA_TOKEN")  # runner Bearer
    runner: str | None = Field(default=None, validation_alias="MNEMA_RUNNER")  # runner URL
    # own CA root for a TLS proxy in front of the server
    ca: str | None = Field(default=None, validation_alias="MNEMA_CA")
    # recording library: enabled only by the server installer (server.env)
    library: bool = Field(default=False, validation_alias="MNEMA_LIBRARY")
    job_workers: int = Field(default=1, ge=1, validation_alias="MNEMA_JOB_WORKERS")
    job_queue_size: int = Field(default=16, ge=0, validation_alias="MNEMA_JOB_QUEUE_SIZE")
    live_connections: int = Field(default=2, ge=1, validation_alias="MNEMA_LIVE_CONNECTIONS")
    deepl_api_key: str | None = Field(default=None, validation_alias="DEEPL_API_KEY")
    live_dir: str | None = Field(default=None, validation_alias="MNEMA_LIVE_DIR")
    # HTTP client (MnemaClient)
    http_connect_timeout: float = Field(default=10.0, validation_alias="MNEMA_HTTP_CONNECT_TIMEOUT")
    http_read_timeout: float = Field(default=60.0, validation_alias="MNEMA_HTTP_READ_TIMEOUT")
    http_write_timeout: float = Field(default=60.0, validation_alias="MNEMA_HTTP_WRITE_TIMEOUT")
    http_pool_timeout: float = Field(default=10.0, validation_alias="MNEMA_HTTP_POOL_TIMEOUT")
    http_retries: int = Field(default=2, validation_alias="MNEMA_HTTP_RETRIES")
    http_retry_backoff: float = Field(default=0.5, validation_alias="MNEMA_HTTP_RETRY_BACKOFF")
