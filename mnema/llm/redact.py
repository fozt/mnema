"""Keys never reach logs, remarks or history: redact and install_redaction.

Known secrets are the model and recognition keys (MNEMA_API_KEY, MNEMA_ASR_API_KEY from the
process environment and ~/.config/mnema/.env), Mnema and Hugging Face tokens;
plus anything that looks like a key.
"""

from __future__ import annotations

import logging
import os
import re
import threading

from .providers import KEY_PREFIXES

# key-like strings: key=… in a URL (litellm puts the Gemini key there) and known key prefixes
_PATTERNS = re.compile(
    "|".join([r"(?<=key=)[\w-]{20,}", *(re.escape(p) + r"[\w-]{20,}" for p, _ in KEY_PREFIXES)])
)
_ENV_SECRETS = ("MNEMA_API_KEY", "MNEMA_ASR_API_KEY", "HF_TOKEN", "MNEMA_TOKEN")
_FILE_SECRETS = ("MNEMA_API_KEY", "MNEMA_ASR_API_KEY")
_FILE: dict[str, tuple[tuple[int, int], list[str]]] = {}  # .env path → (mtime, size), keys
_BUSY = threading.local()  # logging while reading the file must not read it again
_ADDED: set[str] = set()  # keys entered in this process (mnema model) and not yet saved


def _file_secrets() -> list[str]:
    """Model keys from ~/.config/mnema/.env, re-read only when the file changes."""
    from .. import config

    path = config.user_env_file()
    try:
        st = path.stat()
        stamp = st.st_mtime_ns, st.st_size
    except OSError:
        return []
    cached = _FILE.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    if getattr(_BUSY, "on", False):
        return cached[1] if cached else []
    _BUSY.on = True
    try:
        from .config import file_values

        values = file_values()
    finally:
        _BUSY.on = False
    keys = [values[name] for name in _FILE_SECRETS if name in values]
    _FILE[str(path)] = stamp, keys
    return keys


def add_secret(secret: str) -> None:
    """Hide a key that is not in the environment or .env yet (it is checked before saving)."""
    _ADDED.add(secret)


def _known_secrets() -> list[str]:
    env = [os.environ.get(name) or "" for name in _ENV_SECRETS]
    return [s for s in (*_file_secrets(), *env, *_ADDED) if len(s) >= 8]


def mask(secret: str) -> str:
    """ "…a1B2": the key's last four characters; a short key shows none."""
    return f"…{secret[-4:]}" if len(secret) > 8 else "…"


def redact(text: str) -> str:
    """Known secrets and key-like strings (key=…, a provider key prefix) → "…a1B2"."""
    if not text:
        return text
    for secret in sorted(_known_secrets(), key=len, reverse=True):
        text = text.replace(secret, mask(secret))
    return _PATTERNS.sub(lambda m: mask(m.group(0)), text)


_INSTALLED: list[bool] = []


def install_redaction() -> None:
    """logging.setLogRecordFactory: a record's message and exception text go through redact
    (args change only when the message held a secret, since formatters like uvicorn read
    them); calling it again does nothing."""
    if _INSTALLED:
        return
    _INSTALLED.append(True)
    base = logging.getLogRecordFactory()
    formatter = logging.Formatter()

    def factory(*args, **kwargs) -> logging.LogRecord:
        record = base(*args, **kwargs)
        try:
            message = record.getMessage()
            if (clean := redact(message)) != message:
                record.msg, record.args = clean, ()
            if record.exc_info and not record.exc_text:
                record.exc_text = redact(formatter.formatException(record.exc_info))
        except Exception:  # noqa: BLE001 - logging never crashes the program
            pass
        return record

    logging.setLogRecordFactory(factory)
