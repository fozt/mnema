"""The node's name and capabilities. Clients see the chosen model and the available
recognition; the model key and CPU threads are set only in the server's own configuration.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
import unicodedata
from pathlib import Path

import httpx

from .core import base
from .core.base import MediaError
from .i18n import _
from .llm import asr, providers
from .llm.providers import TITLES, Provider

NAME_MAX = 40
UNAVAILABLE_ASR = "The selected recognition is unavailable. Choose another"
# The installed server's default name is data: stored in history and compared by clients
# (remarks, the app), so it stays as it always was; clients get it translated (shown_name).
SERVER_NAME = "Сервер"
DEFAULT_SERVER = "Server"
COMPUTER_NAME = "Computer"  # the computer's name is unknown
_BAD_CHARS = {"Cc", "Zl", "Zp"}  # control characters and line breaks


def node_file() -> Path:
    from .config import config_dir

    return config_dir() / "node.json"


def given_name() -> str | None:
    """The name set in node.json, or None."""
    try:
        saved = json.loads(node_file().read_text(encoding="utf-8")).get("name")
    except (OSError, ValueError, AttributeError):
        return None
    return saved if isinstance(saved, str) and saved.strip() else None


def machine_name() -> str:
    """The computer's name without the domain ("studio.local" → "studio"); empty → "Computer"."""
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    host = "".join(c for c in host.split(".")[0] if unicodedata.category(c) not in _BAD_CHARS)
    return host.strip()[:NAME_MAX] or _(COMPUTER_NAME)


def name() -> str:
    """The node name as stored: the one set in the config, else SERVER_NAME for an installed
    server and the computer's name for a server without a library (a manual `mnema serve`).
    Read on every call: the node gives a new name without a restart."""
    from .config import Settings

    # only the server image turns the recordings library on (MNEMA_LIBRARY in the Dockerfile)
    return given_name() or (SERVER_NAME if Settings().library else machine_name())


def shown_name() -> str:
    """name() for other devices: the default name in the language of whoever asks."""
    node = name()
    return _(DEFAULT_SERVER) if node == SERVER_NAME else node


def is_default_name(node: str) -> bool:
    """The server's default name, as stored or as shown in either language."""
    return node in (SERVER_NAME, DEFAULT_SERVER)


def model() -> str | None:
    """The node's model for clients, the same for /api/capabilities (llm), /api/node/settings
    and the app window: its name when set with a key (LLMSettings.cloud), else None; the key
    is never given out."""
    from .llm.config import LLMSettings

    settings = LLMSettings()
    return settings.model if settings.cloud else None


def current() -> dict:
    """GET /api/node/settings."""
    from .core.backends import engines_available

    return {
        "name": shown_name(),
        "model": model(),
        "engines": engines_available(),
    }


_ASR_CACHE: dict[tuple[Provider, str], tuple[float, list[str]]] = {}
_ASR_LOCK = threading.Lock()


def _local_asr() -> list[dict[str, str]]:
    from .core.backends import installed_engines

    return [{"id": engine.id, "title": engine.title} for engine in installed_engines()]


def local_asr_choices() -> list[dict[str, str]]:
    """Recognition by this machine's engines: Automatic and the installed ones; none installed
    gives an empty list. No network: a Mac has no cloud recognition."""
    choices = _local_asr()
    return [{"id": "auto", "title": _("Automatic")}, *choices] if choices else []


def asr_problem(choice: str, lang: str | None = None) -> str | None:
    """The check before work (before the server claims uploads): only packages and saved keys.
    Processing on this Mac uses only its engines: a cloud choice is refused even with a key;
    the cloud is for the server."""
    if "/" not in choice:
        if choice == "auto":
            from .core.backends import select_backend

            try:
                if select_backend("auto", lang=lang).available():
                    return None
            except MediaError:
                pass
        elif any(c["id"] == choice for c in _local_asr()):
            return None
        return _(UNAVAILABLE_ASR)
    if base.processed_here():
        return _(UNAVAILABLE_ASR)
    found = providers.provider_of(choice)
    if found is None or found[0] not in providers.ASR_PROVIDERS or not found[1]:
        return _("Unknown recognition method")
    provider = found[0]
    return None if asr.provider_key(provider) is not None else asr.no_key(provider)


def asr_choices() -> list[dict[str, str]]:
    """The server's recognition list (GET /api/node/asr): local engines and the models of the
    node's keys; a failed request keeps the previous list."""
    from .llm.config import LLMSettings

    choices = local_asr_choices()
    settings = LLMSettings()
    for provider in providers.ASR_PROVIDERS:
        secret = settings.asr_key(provider)  # the same key recognition uses
        if secret is None:
            continue
        cache_key = provider, hashlib.sha256(secret.encode()).hexdigest()
        with _ASR_LOCK:
            now = time.monotonic()
            previous = _ASR_CACHE.get(cache_key)
            if previous is not None and now - previous[0] < 600:
                models = previous[1]
            else:
                try:
                    models = providers.asr_models(provider, secret, timeout=5.0)
                except (providers.ProviderError, httpx.HTTPError):
                    models = previous[1] if previous else []
                _ASR_CACHE[cache_key] = now, models
        choices.extend(
            {"id": f"{provider.value}/{model}", "title": f"{TITLES[provider]} · {model}"}
            for model in models
        )
    return choices
