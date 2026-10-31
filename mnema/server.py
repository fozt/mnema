"""FastAPI runner: REST over the core, a headless composition root.

Builds the app, wires app.state and registers the routes from mnema.api.
"""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

from fastapi import FastAPI

from . import ops
from .api.auth import add_auth_middleware
from .api.devices import register_device_routes
from .api.files import register_file_routes
from .api.jobs import register_job_routes
from .api.lang import add_language_middleware
from .api.library import Libraries, register_library_routes
from .api.live import register_live_routes
from .api.live_translate import register_live_translate_routes
from .api.model import register_model_routes
from .api.node import register_node_routes
from .api.storage import register_storage_routes
from .api.uploads import Uploads
from .core.base import MediaError
from .devices import DeviceStore, devices_db
from .i18n import _
from .job_queue import JobQueue
from .jobs import RESULT_TTL_DAYS
from .library.files import LibraryFiles
from .repo import create_repos

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
RESTARTED = "The server restarted during the job. Start it again"


def require_safe_bind(host: str, token: str | None) -> None:
    """Refuses a non-loopback host without a token.

    A public bind without a token would open the API to everyone; loopback stays allowed for
    local development. create_app cannot apply it (only the entry point knows the host), so
    every entry point that listens on the network must call it (now `cmd_serve`).
    """
    if host not in _LOOPBACK_HOSTS and not token:
        raise MediaError(
            _(
                "serve on {host} without MNEMA_TOKEN is unsafe: set MNEMA_TOKEN or listen on "
                "127.0.0.1",
                host=host,
            )
        )


def _sweep_outputs(outputs: Path) -> None:
    """Removes FILE result folders older than RESULT_TTL_DAYS, the jobs' own lifetime: a job is
    visible as long as its result can be downloaded."""
    import shutil
    import time

    if not outputs.is_dir():
        return
    cutoff = time.time() - RESULT_TTL_DAYS * 86400
    removed = 0
    for d in outputs.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    if removed:
        logging.getLogger(__name__).info(
            "outputs/: removed %d folders older than %dd", removed, RESULT_TTL_DAYS
        )


def create_app(root: Path) -> FastAPI:
    from .config import Settings, load_env

    load_env()
    settings = Settings()
    # Headless API: Swagger/ReDoc/OpenAPI are off. The auth middleware guards only /api/*, and
    # a public runner has no reason to publish its schema.
    app = FastAPI(title="mnema", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.root = root.resolve()
    app.state.library = LibraryFiles(app.state.root / "library") if settings.library else None
    app.state.libraries = Libraries(app.state.root, app.state.library)
    mnema_dir = app.state.root / ".mnema"
    mnema_dir.mkdir(parents=True, exist_ok=True)
    work = mnema_dir / "work"
    if work.is_symlink():
        work.unlink()
    elif work.exists():
        shutil.rmtree(work)
    repos = create_repos(mnema_dir)
    # The process may have died mid-job (deploy, OOM): close such jobs with an honest error
    # instead of a RUNNING the client would poll forever.
    swept = repos.jobs.sweep_running(RESTARTED)
    # outputs/<job-id>/ holds FILE results for /api/download; without a TTL the disk would fill
    # up (compress leaves gigabytes).
    _sweep_outputs(app.state.root / "outputs")
    app.state.uploads = Uploads(app.state.root)
    app.state.uploads.sweep()
    app.state.job_queue = JobQueue(settings.job_workers, settings.job_queue_size)
    app.state.live_slots = threading.BoundedSemaphore(settings.live_connections)
    if swept:
        logging.getLogger(__name__).warning("closed %d job(s) interrupted by a restart", swept)
    app.state.store = repos.jobs
    app.state.devices = DeviceStore(devices_db(app.state.root))
    app.state.operations = ops.registry()  # dict[str, OpSpec]
    app.state.cancels = set()  # ids of jobs asked to cancel (cooperative, see run_job)
    add_auth_middleware(app)
    add_language_middleware(app)
    register_storage_routes(app)
    register_library_routes(app)
    register_job_routes(app)
    register_file_routes(app)
    register_live_routes(app)
    register_live_translate_routes(app)
    register_node_routes(app)
    register_model_routes(app)
    register_device_routes(app)
    return app
