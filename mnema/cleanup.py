"""Weights of the engines that are gone (Whisper, GigaAM, torch Parakeet): removed once, by their
exact folders, on the Mac app's and the server's start."""

from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path

from .core.base import cache_home

logger = logging.getLogger(__name__)

# Hugging Face models of earlier versions: torch and MLX Parakeet, mlx-whisper, whisperX
_HF_REPOS = ("moondream/parakeet-ultra", "mlx-community/whisper-large-v3-turbo")
# whisperX was Linux only: on a Mac these are other programs' models
_SERVER_HF_REPOS = (
    "Systran/faster-whisper-large-v3",
    "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
)
_DONE = ".old-weights-removed"


def _hub() -> Path:
    """The Hugging Face hub cache, as huggingface_hub finds it."""
    given = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if given:
        return Path(given)
    home = os.environ.get("HF_HOME")
    return (Path(home) if home else cache_home() / "huggingface") / "hub"


def old_weights(*, server: bool) -> list[Path]:
    """The folders to remove. whisperX models and the torch and openai-whisper caches only on
    the server: there the cache folder is Mnema's alone, on a Mac other programs keep their
    models in them."""
    hub = _hub()
    repos = (*_HF_REPOS, *_SERVER_HF_REPOS) if server else _HF_REPOS
    paths = [hub / ("models--" + repo.replace("/", "--")) for repo in repos]
    paths.append(cache_home() / "mnema" / "gigaam")
    if server:
        torch = os.environ.get("TORCH_HOME")
        paths += [Path(torch) if torch else cache_home() / "torch", cache_home() / "whisper"]
    return paths


def remove_old_weights(*, server: bool) -> None:
    """Remove the old weights unless done before; what could not go is tried on the next start."""
    done = cache_home() / "mnema" / _DONE
    if done.exists():
        return
    left = False
    for path in old_weights(server=server):
        if not path.exists():
            continue
        logger.info("removing old recognition weights: %s", path)
        shutil.rmtree(path, ignore_errors=True)
        left = left or path.exists()
    if left:
        return
    try:
        done.parent.mkdir(parents=True, exist_ok=True)
        done.touch()
    except OSError:
        logger.warning("could not mark the old weights as removed", exc_info=True)


def remove_old_weights_in_background(*, server: bool) -> None:
    """The same without holding up the start: gigabytes may take a while."""
    threading.Thread(
        target=remove_old_weights, kwargs={"server": server}, name="old-weights", daemon=True
    ).start()
