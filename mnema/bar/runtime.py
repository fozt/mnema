"""Menu-bar recording lifecycle: recording in a background worker, the processing queue, stop.

GUI-agnostic: every change is reported through on_change, called FROM BACKGROUND THREADS (the
GUI layer hops to its main thread itself); platform actions (notifications, opening files and
folders, windows) come through Platform. Building and finalizing a recording is shared with
`mnema rec` (live/setup.py): stream=False, WAV tracks hold the audio, the text comes from an
offline pass after stop.
"""

from __future__ import annotations

import json
import logging
import platform as _platform
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from ..config import library_root
from ..core import base as _base
from ..core.backends import is_cloud
from ..core.base import MediaError
from ..i18n import _, _n
from ..library.models import FileName
from ..live import meta
from ..live.remarks import SOURCE_NAMES
from ..live.setup import FinalizeCancelled
from ..live.sources import MicUnavailable
from ..node_settings import SERVER_NAME
from ..sync.engine import SyncStatus
from ..sync.scan import FILES_LOCK
from ..sync.state import checkpoint_path
from .forms import FormEvent, KeyFlow, ModelFlow, ServerFlow
from .protocol import FormAsk, FormOp
from .range_pick import LANGS, RangeAnswer, RangeRequest, SourceChoice
from .recordings import find_recording, record_sections
from .settings_model import HOTKEY_BUSY, HOTKEY_INVALID, recognition_at, server_label
from .state import (
    STEP_AT,
    SUMMARIZING,
    BarPrefs,
    BarState,
    Job,
    JobKind,
    MenuEnv,
    Phase,
    Recognition,
    SourceMeter,
    asr_at,
    asr_options,
    can_relang,
    cloud_models,
    load_prefs,
    result_name,
    save_prefs,
    valid_hotkey,
)
from .sync_controller import SyncController

if TYPE_CHECKING:
    from ..connection import Connection
    from ..live.remarks import Remark

logger = logging.getLogger(__name__)

_HISTORY = 6  # points in a source level meter
_UPDATE_EVERY = 12 * 3600  # Mnema update check interval, s (as on the phone)
_UPDATE_RETRY = 3600  # the check failed (no network yet after login): try again after, s
_REPING_EVERY = 30.0  # the server did not answer: ask again at most this often, s
# English source strings, translated where shown
QUEUED_ALREADY = "This recording is already queued"
STILL_RECORDING = "This recording is still in progress. You can process it after it stops"
QUIT_TITLE = "Quit Mnema?"
QUIT_RECORDING = "The recording will stop and be saved. You can process it after the next launch."
QUIT_PROCESSING = (
    "Processing will be interrupted. You can process the recordings after the next launch."
)
QUIT_BOTH = (
    "The recording will stop and be saved, and processing will be interrupted. "
    "You can process the recordings after the next launch."
)


def _failure_text(e: BaseException, failed: str) -> str:
    """A processing failure as a notice: the MediaError text as is, otherwise ``failed`` (a
    whole sentence that points to the log)."""
    text = str(e).strip() if isinstance(e, MediaError) else ""
    return text.splitlines()[0][:300] if text else failed


def _error_text(e: BaseException) -> str:
    """A failed window or menu action, for the user."""
    if isinstance(e, MediaError):
        return str(e)
    if isinstance(e, OSError):
        return _("Could not save: {error}", error=e.strerror or e)
    return _("Something went wrong. See the Mnema log for details")


# In memory only: a notification from a previous launch leads to the old path
_MOVED: dict[Path, Path] = {}  # recording folder → where it was renamed or moved


def followed(path: Path) -> Path:
    """A recording folder or a file in it whose folder was renamed later (the notification came
    earlier): the same path in the new folder."""
    for _step in range(len(_MOVED)):
        if path.exists():
            break
        if path in _MOVED:
            path = _MOVED[path]
        elif path.parent in _MOVED:
            path = _MOVED[path.parent] / path.name
        else:
            break
    return path


FormUpdate = Callable[[FormAsk | None], None]  # new window state; None closes it


class Flow(Protocol):
    """A window with a check (forms.py): ServerFlow or ModelFlow."""

    closed: bool

    @property
    def checks(self) -> bool:
        """The current step checks over the network: "Checking…" while submit runs."""
        ...

    def ask(self) -> FormAsk: ...

    def submit(self, values: dict[str, str], *, force: bool) -> FormAsk | None: ...


class Platform(Protocol):
    """Platform actions of the GUI layer (the macOS app / GNOME)."""

    def notify(self, title: str, message: str, path: Path | None) -> None: ...

    def open(self, path: Path) -> None: ...

    def ask_title(
        self,
        current: str | None,
        on_done: Callable[[str | None], None],
        *,
        cancel: str = ...,
    ) -> None:
        """The recording title window: on_done(name), or on_done(None) when cancelled; cancel
        is the cancel button label ("Skip" right after processing)."""
        ...

    def ask_section(self, on_done: Callable[[str | None], None]) -> None:
        """The "New section" window (protocol.NEW_SECTION_ASK): on_done(name), or None when
        cancelled."""
        ...

    def confirm(
        self,
        title: str,
        message: str,
        ok: str,
        on_done: Callable[[bool], None],
        *,
        cancel: str = ...,
    ) -> None:
        """System confirmation of a dangerous action ("ok" / "cancel" buttons)."""
        ...

    def choose_folder(self, start: Path, on_done: Callable[[Path | None], None]) -> None:
        """Pick a recording folder (any, not only ~/mnema); None when cancelled."""
        ...

    def ask_range(self, req: RangeRequest, on_done: Callable[[RangeAnswer | None], None]) -> None:
        """The "Process recording" window (range_pick): part, title, full recording to the
        Trash; None when cancelled."""
        ...

    def trash(self, path: Path) -> None:
        """Move to the Trash (not delete for good); raises on failure."""
        ...

    def open_form(self, ask: FormAsk, on_event: Callable[[FormEvent | None], None]) -> FormUpdate:
        """Open a live form window. on_event gets every window action, None when closed.
        Returns update: a new window state or None to close it; callable from any thread."""
        ...

    def open_settings(self, tab: str) -> None:
        """The settings window on tab ``tab`` (a SettingsModel tab id)."""
        ...


class _NullPlatform:
    def notify(self, title: str, message: str, path: Path | None) -> None: ...

    def open(self, path: Path) -> None: ...

    def ask_title(
        self,
        current: str | None,
        on_done: Callable[[str | None], None],
        *,
        cancel: str = "",
    ) -> None:
        on_done(None)

    def ask_section(self, on_done: Callable[[str | None], None]) -> None:
        on_done(None)

    def confirm(
        self,
        title: str,
        message: str,
        ok: str,
        on_done: Callable[[bool], None],
        *,
        cancel: str = "",
    ) -> None:
        on_done(False)  # no GUI: delete nothing

    def choose_folder(self, start: Path, on_done: Callable[[Path | None], None]) -> None:
        on_done(None)

    def ask_range(self, req: RangeRequest, on_done: Callable[[RangeAnswer | None], None]) -> None:
        on_done(None)

    def trash(self, path: Path) -> None:
        raise OSError(_("Trash is unavailable"))

    def open_form(self, ask: FormAsk, on_event: Callable[[FormEvent | None], None]) -> FormUpdate:
        on_event(None)
        return lambda _ask: None

    def open_settings(self, tab: str) -> None: ...


def _system_available() -> bool:
    try:
        from ..live.system_audio import system_source

        return bool(system_source().available())
    except Exception:  # noqa: BLE001 - no pyobjc/pactl: microphone only
        return False


def _drop_unused_tracks(prepared: Any, tracks: list[Any]) -> list[Any]:
    """A source muted for the whole recording (unchecked, never turned on from the menu) wrote
    only silence to stay in sync: its track is dropped, so no extra system.wav is kept or
    transcribed. Turned on even once, the track stays (it is in sync with the others)."""
    unused = [label for label, w in prepared.writers.items() if w.live_bytes == 0 and w.muted]
    if len(unused) == len(prepared.writers):
        return tracks  # everything muted: keep the silence rather than an empty folder
    for label in unused:
        writer = prepared.writers.pop(label)
        writer.path.unlink(missing_ok=True)
    return [t for t in tracks if t.label not in unused]


def _details(folder: Path, waiting: list[Remark] | None) -> Path | None:
    """The "Details" of the last processing as text (remarks.details), in a file next to the log so
    it can be opened: from the folder history; waiting — remarks of an attempt that never
    reached the server (it is not in the history). Nothing to say — None."""
    from ..live import remarks
    from .install import log_path

    if waiting is not None:
        text = remarks.details([r.text for r in waiting], [])
    else:
        try:
            data = json.loads((folder / meta.META_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        text = remarks.details(meta.remarks_of(data), meta.info_of(data))
    if not text:
        return None
    path = log_path().with_name(_("Details.txt"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        logger.warning("details not written to %s", path, exc_info=True)
        return None
    return path


def _sources(folder: Path) -> list[SourceChoice]:
    """Folder tracks for the "Process recording" window: all with audio are checked, the server
    decides whether a track is silent."""
    from ..live.naming import WAV_HEADER

    return [
        SourceChoice(label=label, title=SOURCE_NAMES[label])
        for label in SOURCE_NAMES
        if (wav := folder / f"{label}.wav").is_file() and wav.stat().st_size > WAV_HEADER
    ]


class _Part(BaseModel):
    """The "Process…" choices: which part, own title, whether to name by content, the full
    recording to the Trash (see BarRuntime.recover)."""

    start: float | None = None
    end: float | None = None
    title: Any = None
    auto_name: bool | None = None
    replace: bool = False


class _Task:
    """A queue task: the Job is what the panel and the menu see; the rest controls it."""

    def __init__(
        self,
        job: Job,
        *,
        prepared: Any = None,
        tracks: list[Any] | None = None,
        part: _Part | None = None,
        asr: str | None = None,
    ) -> None:
        self.job = job
        self.prepared = prepared  # a just-finished recording: its build
        self.tracks = tracks  # its tracks, or those picked in "Process recording"
        self.part = part or _Part(title=...)
        self.taken = False  # the processing thread has taken it
        self.asr = asr  # recognition for this recording; None — from settings when it starts
        self.stop = False  # "Stop processing": keep the audio, make no text
        self.restart = False  # the language was corrected: interrupt and start over
        self.named_by_user = False  # the user gave a title: no title window afterwards
        # given while there was no work file (the result is applied): it names the ready folder
        self.late_title: str | None = None
        self.since: float | None = None  # when it started (monotonic)
        self.began: str | None = None  # the same as processing-history time


class BarRuntime:
    """Recording from the menu: handle(key) — menu items, tick() — timer and levels (GUI timer)."""

    def __init__(
        self,
        *,
        on_change: Callable[[], None] | None = None,
        platform: Platform | None = None,
        prefs_file: Path | None = None,
    ) -> None:
        self.state = BarState()
        self._prefs_file = prefs_file
        self.prefs: BarPrefs = load_prefs(prefs_file)
        try:  # "Open at login" follows the autostart file, not the saved setting
            from .install import autostart_enabled

            self.prefs.autostart = autostart_enabled()
        except Exception:  # noqa: BLE001
            pass
        self._on_change = on_change or (lambda: None)
        self._platform: Platform = platform or _NullPlatform()
        self._prepared: Any = None
        self._session: Any = None
        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self._cancel = False
        self._tasks: list[_Task] = []  # the processing queue; the first runs (state.jobs shows it)
        self._worker: threading.Thread | None = None  # the "mnema-bar-process" thread
        self._started = 0.0
        self._paused_at: float | None = None
        self._paused_total = 0.0
        # title and section windows open one at a time; the rest wait as (key, show)
        self._prompt_lock = threading.Lock()
        self._prompt_open: str | None = None
        self._prompts: deque[tuple[str, Callable[[Callable[[], None]], bool]]] = deque()
        self._active: Path | None = None  # the folder being recorded
        # a recording whose processing never reached the server → "Retry": the same processing
        self._retries: dict[Path, Callable[..., None]] = {}
        # lock order: FILES_LOCK, then self._lock, never the reverse (renaming holds FILES_LOCK
        # and looks the task up under self._lock)
        self._lock = threading.Lock()
        self.server_ok = False
        self.server_auth = True  # False: the server answered 401/403
        self.server_name: str | None = None  # the server node name from capabilities
        self.server_llm = False  # the server node writes titles and summaries itself
        self.server_version: str | None = None  # the server's Mnema version from capabilities
        self.server_model: str | None = None  # the user's model on it, without the key (Mac)
        self.server_asr_key = False  # the user's cloud recognition key is on it (Mac)
        self.server_deepl = False  # the user's DeepL key is on it (Mac)
        self.hotkey_error = ""  # the shortcut did not fit or could not be registered
        self.env_rev = 0  # grows when the menu environment changes (models, server, token)
        self._asr_choices: list[tuple[str, str]] = []  # the engines of the recognition place
        self._asr_stale = False  # the server list was not read: reread once it answers
        self._ping_at = float("-inf")  # when the server was last asked (monotonic)
        self._update = ""  # the newer version (MenuEnv.update)
        self._update_error = ""  # the last check or update failed: not a "latest version" answer
        self._update_step = ""  # an update is running: its step
        self._update_progress: float | None = None  # the step's share (the download)
        self._update_checking = False  # a check is running
        # when the next update check is due; wall clock: monotonic time stops while the Mac sleeps
        self._update_next = 0.0
        self._update_checked = False  # a check has finished at least once
        self._update_busy = False  # a check or an update is running
        self.quit_requested = False
        self.library_rev = 0
        self._sync: SyncController | None = None
        self._sync_connection_status: str | None = None
        # sync.json as read for "Recordings on the server": (file stamp, server, records)
        self._records_seen: tuple[tuple[int, int], str, int] | None = None
        self._check_server_async()
        self._load_asr_choices()
        self._scan_unfinished()

    @property
    def active_folder(self) -> Path | None:
        """The folder being recorded (it is not in the recordings list); the processing and
        waiting ones are in the list at their places. The path comes from the build: processing
        may have renamed the folder already."""
        prepared = self._prepared
        return prepared.base if prepared is not None else self._active

    def job_folders(self) -> list[Path]:
        """Folders of the processing queue: running and waiting (a card even outside the
        library)."""
        return [job.folder for job in self.state.jobs]

    def sync_connection(self) -> str | None:
        """The explicitly saved library server, independent of where processing runs."""
        from ..config import Settings
        from ..connection import format_connection

        settings = Settings()
        return format_connection(settings.runner, settings.token) if settings.runner else None

    def start_sync(self) -> None:
        """Once the GUI is ready: the first pass and periodic sync of plain files."""
        if self._sync is None:
            self._sync = SyncController(
                library_root(),
                checkpoint_path(),
                selected=self.sync_connection,
                busy=self._sync_busy,
                trash=self._platform.trash,
                on_change=self._sync_changed,
            )
            self._sync.start()

    def _sync_busy(self) -> set[str]:
        """Hands off: the folder being recorded and the one being processed."""
        return self._busy(queued=False)

    def _held(self) -> set[str]:
        """Audio cleanup and the "Not processed" list also skip queued recordings."""
        return self._busy(queued=True)

    def _busy(self, *, queued: bool) -> set[str]:
        with FILES_LOCK:
            busy: set[str] = set()
            if (active := self.active_folder) is not None:
                busy.add(str(active.absolute()))
                try:
                    from ..live.meta import META_FILE

                    data = json.loads((active / META_FILE).read_bytes())
                    if isinstance(data, dict) and isinstance(data.get("id"), str):
                        busy.add(data["id"])
                except (OSError, ValueError):
                    pass
            for job in self.state.jobs:
                if job.running or queued:
                    busy |= {str(job.folder.absolute()), job.id}
            return busy

    def _sync_changed(self, status: SyncStatus) -> None:
        selected = self.sync_connection()
        if status.connection != selected:
            status = SyncStatus(connection=selected)  # an answer of a server already removed
        if status.connection != self._sync_connection_status:
            self.state.sync_revision += 1
        self._sync_connection_status = status.connection
        self.state.sync_missing = list(status.bulk_missing)
        self.state.sync_missing_on_server = status.bulk_on_server
        self.state.sync_other_library = status.other_library
        self.state.sync_foreign = status.foreign
        self.state.sync_errors = list(status.errors)
        if status.changed:
            self.library_rev += 1
        self._on_change()

    def _library_changed(self) -> None:
        self.library_rev += 1
        if self._sync is not None:
            self._sync.wake()
        self._on_change()

    # ── environment ────────────────────────────────────────────────────────────

    @property
    def server(self) -> str | None:
        """The connected server (the "Server" window, MNEMA_RUNNER), wherever processing runs."""
        from ..config import Settings

        return Settings().runner or None

    def processing_connection(self) -> Connection | None:
        """Where to process (node.selected_processing with this app's choice): a server, or
        None for this computer (a Mac recognizes itself unless the server or the cloud with a
        connected server is chosen; other systems cannot process without a server)."""
        from ..node import selected_processing

        return selected_processing(recognition=self.prefs.recognition)

    def summary_connection(self) -> Connection | None:
        """Who writes titles and summaries: the processing server, else the connected server
        of a Mac (the user's model there), else this Mac."""
        from ..node import summary_server

        return self.processing_connection() or summary_server()

    def _place(self) -> Recognition:
        """The place that recognizes now (settings_model.recognition_at)."""
        return recognition_at(self.prefs, MenuEnv(mac=_base.STANDALONE, server=self.server))

    def _load_asr_choices(self) -> threading.Thread | None:
        """The engines of the recognition place in the background: this Mac lists its own,
        offline; the server and the cloud with a server come from the server's list (cloud models
        are the "openai/" ones), empty on error; the cloud without a server is this Mac's cloud
        engine. Providers are never asked."""
        place, connection = self._place(), self.processing_connection()
        self._asr_choices = []
        if connection is None and not _base.STANDALONE:
            return None

        def choices() -> list[tuple[str, str]]:
            if connection is None:
                from .. import node_settings

                if place == Recognition.CLOUD:
                    return cloud_models()
                return [(c["id"], c["title"]) for c in node_settings.local_asr_choices()]
            from ..client import MnemaClient

            found = MnemaClient(connection.url, token=connection.token).asr_choices()
            listed = [(c["id"], c["title"]) for c in found]
            return [(k, t) for k, t in listed if is_cloud(k) == (place == Recognition.CLOUD)]

        def load() -> None:
            try:
                found = choices()
            except Exception:  # noqa: BLE001 - the saved choice stays visible without a list
                logger.info("could not read the recognition list")
                found = None
            if (self._place(), self.processing_connection()) == (place, connection):
                self._asr_stale = found is None and connection is not None
                self._asr_choices = found or []
                self._env_changed()

        thread = threading.Thread(target=load, daemon=True, name="mnema-bar-asr-choices")
        thread.start()
        return thread

    def menu_env(self) -> MenuEnv:
        """Connection, the models and the recognition list, without keys."""
        from .. import node_settings
        from ..llm.config import LLMSettings

        server = self.server
        here = _base.STANDALONE and server is None
        return MenuEnv(
            server=server,
            server_ok=self.server_ok,
            server_auth=self.server_auth,
            server_name=self.server_name,
            server_llm=self.server_llm,
            server_model=self.server_model,
            server_deepl=self.server_deepl,
            server_records=self._server_records(),
            server_version=self.server_version,
            model=node_settings.model() if here else None,
            mac=_base.STANDALONE,
            asr_choices=self._asr_choices,
            asr_key=self.server_asr_key if server else here and bool(LLMSettings().asr_api_key),
            update=self._update,
            update_error=self._update_error,
            update_step=self._update_step,
            update_progress=self._update_progress,
            update_checking=self._update_checking,
            update_checked=self._update_checked,
        )

    def _server_records(self) -> int | None:
        """Records in the last sync (sync.json) if it was with the connected server."""
        from ..sync.state import checkpoint_path, load, server_key

        connection = self.sync_connection()
        if connection is None:
            return None
        path = checkpoint_path()
        try:
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
            if self._records_seen is None or self._records_seen[0] != stamp:
                checkpoint = load(path)
                # another library on the server: the count is not its recordings
                server = "" if checkpoint.foreign else checkpoint.server
                self._records_seen = (stamp, server, len(checkpoint.records))
            _stamp, server, count = self._records_seen
            return count if server == server_key(connection) else None
        except (OSError, ValueError):
            return None

    def _env_changed(self) -> None:
        """The menu environment changed: the engine rereads it now, not on the next tick."""
        self.env_rev += 1
        self._on_change()

    def _scan_unfinished(self) -> None:
        from ..live.setup import unfinished_sessions

        try:
            found = unfinished_sessions(library_root(), exclude=self.active_folder)
        except OSError:
            found = []
        held = self._held()  # queued and processing ones are no longer "Not processed"
        self.state.unfinished = [d for d in found if str(d.absolute()) not in held]

    def _check_server_async(self, *, status: bool = True) -> None:
        """Check the server in the background: does it answer, does it accept the token, its
        node name and whether it summarizes; status=False — the "Server" window has checked the
        connection, only the name and the model. No answer — the name and the model stay (the
        server is not gone, it was not reached) and tick asks again; an answer rereads a
        recognition list that could not be read without it."""
        self._ping_at = time.monotonic()
        url = self.server
        if url is None:
            self.server_ok, self.server_auth = False, True
            self.server_name, self.server_llm, self.server_model = None, False, None
            self.server_version = None
            self.server_asr_key = self.server_deepl = False
            return

        def ping() -> None:
            from ..client import MnemaClient
            from ..config import Settings

            caps = MnemaClient(url, token=Settings().token or None).health()
            auth = caps is None or caps.get("auth") is not False
            ok = caps is not None and auth
            keys = self.server_asr_key, self.server_deepl
            if caps is None:  # no answer: what is known about it stays
                name, llm, model = self.server_name, self.server_llm, self.server_model
                version = self.server_version
            else:
                found = caps.get("version")
                version = found if isinstance(found, str) else None
                name = caps.get("name")
                name = (name.strip()[:40] or None) if isinstance(name, str) else None
                llm = caps.get("llm") is True
                model = None
                if _base.STANDALONE and auth:  # the Mac shows and sets the user's model there
                    known = self._server_model_of(url)
                    if known is not None:
                        model, llm = known["model"], known["model"] is not None
                        keys = known["asr_key"], known["deepl"]
            if ok and self._asr_stale:
                self._load_asr_choices()
            if not status:
                ok, auth = self.server_ok, self.server_auth
            now = (ok, auth, name, llm, model, version, *keys)
            if now != (
                self.server_ok,
                self.server_auth,
                self.server_name,
                self.server_llm,
                self.server_model,
                self.server_version,
                self.server_asr_key,
                self.server_deepl,
            ):
                (
                    self.server_ok,
                    self.server_auth,
                    self.server_name,
                    self.server_llm,
                    self.server_model,
                    self.server_version,
                    self.server_asr_key,
                    self.server_deepl,
                ) = now
                self._env_changed()

        threading.Thread(target=ping, daemon=True, name="mnema-bar-ping").start()

    @staticmethod
    def _server_model_of(url: str) -> dict[str, Any] | None:
        """The user's model on the server and whether its keys are there (GET /api/model);
        unknown — None."""
        from ..client import MnemaClient
        from ..config import Settings

        try:
            found = MnemaClient(url, token=Settings().token or None).model()
        except Exception:  # noqa: BLE001 - the "Model" row stays without a name
            logger.info("the model on the server is unknown")
            return None
        if not isinstance(found, dict):
            return None
        model = found.get("model")
        return {
            "model": (model.strip()[:80] or None) if isinstance(model, str) else None,
            "asr_key": isinstance(found.get("asr_key"), str),
            "deepl": isinstance(found.get("deepl_key"), str),
        }

    @property
    def can_exit(self) -> bool:
        """Quit was asked, the recording is saved and no update is being installed (quitting in
        the middle of uv sync left a half-made version and an orphan installer). The queue is
        dropped on quit and the running processing is interrupted: "Process…" continues it
        after the next launch."""
        return self.quit_requested and self.state.phase == Phase.IDLE and not self._update_busy

    def _check_updates(self) -> None:
        """Check the published Mnema version in the background; a failure goes quietly to the
        log: no update is not an emergency. The first check runs from the first tick, not from
        the constructor: the thread calls on_change, and the object does not exist yet."""
        with self._lock:
            if self._update_busy or self.quit_requested:
                return
            self._update_busy = self._update_checking = True
        self._env_changed()

        def work() -> None:
            from .. import update

            wait = _UPDATE_EVERY
            try:
                found = update.check()
                self._update = found.release.version if found.any and found.release else ""
                self._update_error = ""
            except Exception as e:  # noqa: BLE001 - no network, not from the registry: quietly
                self._update = ""  # the earlier find is stale: no "Update" without a check
                self._update_error = str(e)
                wait = _UPDATE_RETRY
                logger.info("updates not checked: %s", e)
            finally:
                self._update_next = time.time() + wait
                self._update_checked = True
                self._update_busy = self._update_checking = False
                self._env_changed()

        threading.Thread(target=work, daemon=True, name="mnema-bar-update-check").start()

    def _apply_update(self) -> None:
        """The "Update" button: a separate app version (update.apply), then a relaunch. A
        failure — the reason as a notice, the working version stays intact."""
        with self._lock:
            if self._update_busy or self.quit_requested or self.state.busy:
                return  # the update waits for both recording and the processing queue
            self._update_busy = True

        def step(share: float | None, message: str) -> None:
            self._update_step, self._update_progress = message, share
            self._env_changed()

        def work() -> None:
            from .. import update

            step(None, _("Starting the update…"))
            failure = ""
            try:
                result = update.apply(on=step)
            except Exception as e:  # noqa: BLE001 - the reason goes to the user, the app lives
                logger.warning("update not installed", exc_info=True)
                failure = _failure_text(
                    e, _("Could not update Mnema. See the Mnema log for details")
                )
                self.state.error = failure
                result = None
            with self._lock:
                self._update_step, self._update_progress, self._update_busy = "", None, False
                self._update_next = time.time() + _UPDATE_EVERY
                self._update_error = failure
                if result is not None:
                    self._update = ""
            self._env_changed()
            if result is not None and result.changed:
                self._relaunch(python=result.python, bundle=result.bundle)

        threading.Thread(target=work, daemon=True, name="mnema-bar-update").start()

    def _relaunch(self, *, python: str | None = None, bundle: str | None = None) -> None:
        """Relaunch on the new version: a separate process opens Mnema in 3 s, this one quits.
        bundle: the self-contained Mnema.app that now holds the new version."""
        import shlex
        import subprocess

        from .install import app_path, launch_command

        installed = bool(python or bundle)
        env = None
        if installed:
            from ..update import _env

            env = _env()
            # MNEMA_PYTHON from a terminal launch must not leak into the relaunched app: the
            # updated bundle takes the interpreter and libpython from its own Info.plist
            for name in ("MNEMA_PYTHON", "MNEMA_LIBPYTHON"):
                env.pop(name, None)
        if _platform.system() == "Darwin":  # on a Mac the engine lives only in Mnema.app
            again = f"sleep 3; /usr/bin/open {shlex.quote(str(bundle or app_path()))}"
        else:
            # -I leaves out the old checkout and the user's PYTHONPATH: after a source install
            # the app's current folder may still be the repository
            command = [python, "-I", "-m", "mnema.bar"] if python else launch_command()
            again = f"sleep 3; exec {shlex.join(command)}"
        with self._lock:
            if installed and self.quit_requested:
                return
            if installed and self.state.busy:
                self.state.notice = _(
                    "Update installed. Restart Mnema when recording and processing are done."
                )
            else:
                subprocess.Popen(
                    ["/bin/sh", "-c", again], start_new_session=True, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    cwd=Path.home() if installed else None, env=env,
                )  # fmt: skip
                self._update_step, self._update_progress = "", None
                self.quit_requested = True
        if installed:
            self._env_changed()
        else:
            self._on_change()

    def _save_prefs(self) -> None:
        try:
            save_prefs(self.prefs, self._prefs_file)
        except OSError as e:  # settings are best effort: the recording matters more
            logger.warning("bar settings not saved: %s", e)

    # ── menu actions ───────────────────────────────────────────────────────────

    def handle(self, key: str) -> None:
        """A menu item press (keys come from state.menu)."""
        idle = self.state.phase == Phase.IDLE
        if key.startswith("record:"):
            self._record_action(key)
            return
        if key.startswith(("sync_delete:", "sync_restore:")):
            _kind, revision, values = key.split(":", 2)
            ids = values.split(",")
            if (
                self._sync is not None
                and revision == str(self.state.sync_revision)
                and self.sync_connection() == self._sync_connection_status
                and set(ids) == set(self.state.sync_missing)
            ):
                self._sync.resolve_bulk(ids, restore=key.startswith("sync_restore:"))
            return
        if key.startswith("sync_send:"):
            _kind, revision, library = key.split(":", 2)
            if (
                self._sync is not None
                and revision == str(self.state.sync_revision)
                and self.sync_connection() == self._sync_connection_status
                and self.state.sync_other_library is not None
                and library == self.state.sync_foreign
            ):
                self._sync.send_to_library(library)
            return
        if key == "cleanup_audio" and idle:
            self._cleanup_audio()
            return
        if key == "toggle_record":  # the shortcut starts or stops recording; the processing
            key = "start" if idle else "stop"  # queue does not get in its way
        if key == "start" and self._update_step:
            self.state.notice = _("Mnema is updating. You can start recording after the update")
        elif key == "start":
            self.start()
        elif key == "stop":  # the title window opens after processing
            self.stop()
        elif key == "rename":  # tray: "Rename…" of the last recording
            self._ask_title("last", self._current_title, self.set_title)
        elif key == "pause":
            self._set_paused(True)
        elif key == "resume":
            self._set_paused(False)
        elif key == "discard" and self.state.phase == Phase.RECORDING:
            self._platform.confirm(
                _("Discard recording?"),
                _("The audio will not be saved."),
                _("Discard recording"),
                lambda yes: self.stop(cancel=True) if yes else None,
                cancel=_("Keep recording"),
            )
        elif key == "open_notes" and self.state.last_result is not None:
            from .state import folder_of

            folder = folder_of(self.state.last_result)
            waiting = folder.absolute() in self.state.unreachable
            details = _details(folder, self.state.last_warnings if waiting else None)
            if details is not None:
                self._platform.open(details)
        elif key == "dismiss_notice":  # the panel notice was read
            self.state.error = self.state.notice = ""
        elif key == "server_setup" and idle:
            self._form(ServerFlow())
        elif key == "model_setup" and idle and _base.STANDALONE:  # on the server or this Mac
            server = self.summary_connection()
            self._form(ModelFlow(server, current=self.server_model) if server else ModelFlow())
        elif key == "asr_key_setup" and idle and _base.STANDALONE:
            self._form(KeyFlow("asr", self.summary_connection()))
        elif key == "deepl_setup" and idle and _base.STANDALONE and self.server is not None:
            self._form(KeyFlow("deepl", self.summary_connection()))
        elif key == "choose_server":  # the "<server> is unavailable" card: connect another one
            self._platform.open_settings("server")
        elif key == "server_off" and idle:
            self._ask_server_off()
        elif key == "process_folder":  # while recording too: into the queue
            self._platform.choose_folder(library_root(), self._ask_range_for)
        elif key.startswith("source:"):
            self._toggle_source(key.removeprefix("source:"))
        elif key == "discreet":
            self.prefs.discreet = not self.prefs.discreet
        elif key.startswith("summarize:"):
            self.summarize(Path(key.removeprefix("summarize:")))
        elif key == "toggle_mic" and idle:
            self.prefs.mic = not self.prefs.mic
        elif key == "toggle_system" and idle:
            self.prefs.system = not self.prefs.system
        elif key.startswith("record_section:") and idle:
            name = key.removeprefix("record_section:")
            if name and name not in self.record_sections():
                return  # the section is gone or is not a section: the choice stays
            self.prefs.record_section = name
        elif key == "summary" and idle:
            self.prefs.summary = not self.prefs.summary
        elif key == "open_summary" and self.state.last_summary is not None:
            self._platform.open(self.state.last_summary)
        elif key.startswith("summary_lang:") and idle:
            v = key.removeprefix("summary_lang:")
            self.prefs.summary_lang = None if v == "auto" else v
        elif key.startswith("hotkey:"):  # "hotkey:" clears the shortcut
            spec = key.removeprefix("hotkey:")
            if spec and not valid_hotkey(spec):  # the old shortcut stays, the field shows why
                self.hotkey_error = HOTKEY_INVALID
            else:
                self.prefs.hotkey, self.hotkey_error = spec or None, ""
        elif key.startswith("hotkey_busy:"):  # taken by the system or another app
            if key.removeprefix("hotkey_busy:") != self.prefs.hotkey:
                return  # the shortcut has changed since
            self.prefs.hotkey, self.hotkey_error = None, HOTKEY_BUSY
        elif key == "open_last" and self.state.last_result is not None:
            self._platform.open(self.state.last_result)
        elif key == "reveal_last" and self.state.last_result is not None:  # "Open folder"
            from .state import folder_of

            self._platform.open(folder_of(self.state.last_result))
        elif key.startswith("move_last:") and self.state.last_result is not None:
            from .state import folder_of  # tray: "Move to section" of the last recording

            self.move_folder(folder_of(self.state.last_result), key.removeprefix("move_last:"))
        elif key == "new_section_last" and self.state.last_result is not None:
            from .state import folder_of

            self._ask_section(folder_of(self.state.last_result))
        elif key.startswith("recover:"):  # "Process…": the same window for a folder
            self._ask_range_for(Path(key.removeprefix("recover:")))
        elif key.startswith("reveal:"):  # "Open folder" of a recording: the folder itself
            path = Path(key.removeprefix("reveal:"))
            if path.exists():
                self._platform.open(path if path.is_dir() else path.parent)
        elif key.startswith("open:"):  # a recording file from a card or a notification
            path = followed(Path(key.removeprefix("open:")))
            if path.exists():
                self._platform.open(path)
        elif key == "open_folder":
            self._platform.open(library_root())
        elif key == "autostart":
            from .install import set_autostart

            try:
                self.prefs.autostart = set_autostart(not self.prefs.autostart)
            except Exception as e:  # noqa: BLE001 - show the error, the app lives
                logger.warning("autostart not changed", exc_info=True)
                self.state.error = _failure_text(
                    e, _("Could not change Open at login. See the Mnema log for details")
                )
        elif key.startswith("asr:") and idle:
            self.prefs.asr = key.removeprefix("asr:")
        elif key.startswith("recognition:") and idle:
            if not self._set_recognition(key.removeprefix("recognition:")):
                return
        elif key == "update_check":
            self._check_updates()
        elif key == "update":
            self._apply_update()
        elif key == "quit":  # recording or processing: ask first
            self._ask_quit()
            return  # settings are not written: the installer may have changed them meanwhile
        elif key == "quit_now":  # logout, a signal, the app closed: no question
            self._quit_now()
            return
        else:
            return
        self._save_prefs()
        self._on_change()

    # ── start/stop ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if (
                self.quit_requested
                or self.state.phase != Phase.IDLE
                or not (self.prefs.mic or self.prefs.system)
            ):
                return
            if self._thread is not None and self._thread.is_alive():
                return  # the previous recording is still closing its tracks
            # processing of earlier ones goes on: its queue, last recording and sync stay
            self.state.phase = Phase.RECORDING  # recording comes first: the phase shows at once
            self.state.paused, self.state.elapsed, self.state.meters = False, 0.0, []
            self.state.notice = self.state.error = ""
            self._stop_requested = self._cancel = False
            self._started = time.monotonic()
            self._paused_at, self._paused_total = None, 0.0
            self._thread = threading.Thread(target=self._run, daemon=True, name="mnema-bar-rec")
            self._thread.start()
        self._on_change()

    def record_sections(self) -> list[str]:
        """Library sections a new recording can go straight into."""
        return record_sections(library_root())

    def _record_section(self) -> str:
        """The section of a new recording: gone or no longer a section — the root, and the
        choice becomes "No section"."""
        name = self.prefs.record_section
        if not name or name in self.record_sections():
            return name
        logger.warning('section "%s" is gone, recording into the library root', name)
        self.prefs.record_section = ""
        self._save_prefs()
        return ""

    def _set_paused(self, paused: bool) -> None:
        """Pause: all tracks stop writing at once (they stay in sync), the timer stops."""
        prepared = self._prepared
        if self.state.phase != Phase.RECORDING or self.state.paused == paused or prepared is None:
            return
        for w in prepared.writers.values():
            w.paused = paused
        now = time.monotonic()
        if paused:
            self._paused_at = now
        elif self._paused_at is not None:
            self._paused_total += now - self._paused_at
            self._paused_at = None
        self.state.paused = paused

    def _prompt(self, key: str, show: Callable[[Callable[[], None]], bool]) -> None:
        """Title and section windows one at a time: while one is open the next ones wait (the
        same window only once). show(done) opens a window — done when it is answered — and
        says whether it opened (nothing to ask — the next one)."""
        with self._prompt_lock:
            if self._prompt_open is not None:
                if key != self._prompt_open and all(k != key for k, _show in self._prompts):
                    self._prompts.append((key, show))
                return
            self._prompt_open = key
        self._show_prompt(show)

    def _show_prompt(self, show: Callable[[Callable[[], None]], bool]) -> None:
        answered = threading.Event()

        def done() -> None:
            if not answered.is_set():  # one answer only
                answered.set()
                self._next_prompt()

        try:
            opened = show(done)
        except Exception:
            done()
            raise
        if not opened:
            done()

    def _next_prompt(self) -> None:
        with self._prompt_lock:
            if not self._prompts:
                self._prompt_open = None
                return
            key, show = self._prompts.popleft()
            self._prompt_open = key
        self._show_prompt(show)

    def _ask_title(
        self,
        key: str,
        current: Callable[[], str | None],
        on_done: Callable[[str | None], None],
        *,
        cancel: str | None = None,
        when: Callable[[], bool] = lambda: True,
    ) -> None:
        """The title window (key — which one, in the window queue); the current title and when
        (whether to ask at all) are taken when the window opens; cancel — the cancel label."""

        def show(done: Callable[[], None]) -> bool:
            if not when():
                return False

            def answered(value: str | None) -> None:
                try:
                    on_done(value)
                finally:
                    done()

            self._platform.ask_title(current(), answered, cancel=cancel or _("Cancel"))
            return True

        self._prompt(key, show)

    def _own_title(self, folder: Path | None) -> str | None:
        """The own title of a folder's running processing, from its work file."""
        from ..live.recording_client import own_title

        try:
            return own_title(folder) if folder is not None else None
        except (OSError, MediaError):
            logger.warning("own title of %s is unreadable", folder, exc_info=True)
            return None

    def _current_title(self) -> str | None:
        last = self.state.last_result
        if last is not None:
            from .state import title_of

            return title_of(last)
        return None

    def set_title(self, name: str | None) -> None:
        """The user's title for the last recording (None — the window was closed): rename its
        folder. The own title of a recording being processed is _set_job_title."""
        if name is None:
            return
        if self.state.last_result is not None:
            from .state import folder_of

            self.rename_folder(folder_of(self.state.last_result), name.strip())

    def _task(self, record_id: str) -> _Task | None:
        with self._lock:
            return next((t for t in self._tasks if t.job.id == record_id), None)

    def _record_action(self, key: str) -> None:
        """A card action: the recording is looked up in the processing queue first (its folder
        may be outside the library), then in the library."""
        from .state import title_of

        _kind, id, action = key.split(":", 2)
        task = self._task(id)
        if action in ("stop", "title") or action.startswith("lang:"):
            if task is not None:  # processing is over: nothing to do
                self._job_action(task, action)
            return
        folder = task.job.folder if task is not None else find_recording(library_root(), id)
        if folder is None or not folder.is_dir():
            self.state.error = _("Recording not found")
            self._library_changed()
            return
        inside = folder.resolve().is_relative_to(library_root().resolve()) or task is not None
        if action == "trash":
            self.trash_folder(folder, record_id=id)
        elif action == "rename":
            self._ask_title(
                f"rename:{id}",
                lambda: title_of(self._folder_of(id) or folder),
                lambda name: self.rename_folder(folder, name, record_id=id),
            )
        elif action.startswith("move:"):
            self.move_folder(folder, action.removeprefix("move:"), record_id=id)
        elif action == "new_section":
            self._ask_section(folder, record_id=id)
        elif action == "process":
            self._ask_range_for(folder, record_id=id)
        elif action == "summarize":
            self.summarize(folder, record_id=id)
        elif action == "retry":
            self._retry(folder, id)
        elif action == "folder":  # a click on the card and "Open folder": the folder itself
            if inside:
                self._platform.open(folder)
        elif action in ("transcript", "summary"):
            path = folder / (FileName.TRANSCRIPT if action == "transcript" else FileName.SUMMARY)
            if path.is_file() and inside:
                self._platform.open(path)

    def _job_action(self, task: _Task, action: str) -> None:
        """A queued card: "Stop processing" / "Remove from queue", language, own title."""
        job = task.job
        if action == "stop":
            unqueued = False
            with self._lock:
                if job.running or task.taken:  # the audio stays, the history gets "stopped"
                    task.stop, task.restart = True, False
                elif task in self._tasks:  # waiting: the history and the recording stay as is
                    self._tasks.remove(task)
                    self._sync_jobs()
                    unqueued = True
            if unqueued:  # "Not processed" again
                self._scan_unfinished()
                self._library_changed()
            else:
                self._on_change()
        elif action.startswith("lang:") and can_relang(job):
            self._relang(task, action.removeprefix("lang:"))
            self._on_change()
        elif action == "title" and can_relang(job):
            self._ask_title(
                f"title:{job.id}",
                lambda: task.late_title or self._own_title(job.folder),
                partial(self._set_job_title, task),
            )

    def _set_job_title(self, task: _Task, name: str | None) -> None:
        """The own title of a recording being processed goes to its work file (processing names
        the folder with it instead of its own; empty clears it); no work file yet — the title
        names the folder when processing ends; processing is over — rename the folder now."""
        if name is None:
            return
        from ..live.naming import chosen_title, part_label
        from ..live.recording_client import set_own_title

        folder = task.job.folder
        chosen = chosen_title(name.strip(), part_label(folder.name))
        with self._lock:
            done = task not in self._tasks
        if done:
            if chosen is not None:
                self.rename_folder(followed(folder), chosen)
            return
        try:
            written = set_own_title(folder, chosen)
        except (OSError, MediaError):
            logger.warning("own title of %s not saved", folder, exc_info=True)
            written = False
        task.late_title = None if written else chosen
        task.named_by_user = chosen is not None
        self._on_change()

    def _cleanup_audio(self) -> None:
        """Deleting audio of processed recordings: the question and the deletion share one
        connection snapshot: without a server the texts stay only here, and the question
        says so."""
        from .recordings import cleanup_audio, cleanup_plan, size_label

        connection = self.sync_connection()

        def confirmed(yes: bool) -> None:
            if not yes or self.state.phase != Phase.IDLE:
                return
            result = cleanup_audio(
                library_root(),
                connection=connection,
                busy=self._held,  # queued and processing recordings are left alone
                trash=self._platform.trash,
            )
            self.state.notice = _("Audio deleted from recordings: {n}", n=result.removed)
            self.state.error = "\n".join(result.errors)
            self._library_changed()

        plan = cleanup_plan(library_root(), connection=connection, busy=self._held)
        if not plan.records:  # why there is nothing to delete: a notice, not silence
            self.state.notice = plan.reason
            return
        size = size_label(plan.bytes)
        if connection:
            message = _("This frees {size}. The texts stay.", size=size)
        elif _base.STANDALONE:
            message = _("This frees {size}. The texts stay only on this Mac.", size=size)
        else:
            message = _("This frees {size}. The texts stay only on this computer.", size=size)
        self._platform.confirm(
            _n("Delete audio of {n} recording?", "Delete audio of {n} recordings?", plan.records),
            message,
            _("Delete"),
            confirmed,
        )

    def trash_folder(self, folder: Path, *, record_id: str | None = None) -> None:
        """The whole recording to the Trash (it can be restored from Finder); not one being
        recorded or processed; a waiting one leaves the queue first."""
        from .state import folder_of

        try:
            with FILES_LOCK:
                if record_id is not None:
                    task = self._task(record_id)
                    current = (
                        task.job.folder
                        if task is not None
                        else find_recording(library_root(), record_id)
                    )
                    if current is None:
                        return
                    folder = current
                if not folder.is_dir() or str(folder.absolute()) in self._sync_busy():
                    return
                with self._lock:  # a waiting one leaves the queue; one already taken stays
                    if any(t.taken and t.job.folder == folder for t in self._tasks):
                        return
                    self._tasks = [t for t in self._tasks if t.job.folder != folder]
                    self._sync_jobs()
                self._platform.trash(folder)
        except OSError:
            logger.warning("recording %s not deleted", folder, exc_info=True)
            self.state.error = _("Could not delete the recording. See the Mnema log for details")
            return
        last = self.state.last_result
        if last is not None and folder_of(last) == folder:
            self.state.last_result = None
            self.state.last_warnings = []
            self.state.last_summary = None
        self._scan_unfinished()
        self._library_changed()

    def rename_folder(
        self, folder: Path, name: str | None, *, record_id: str | None = None
    ) -> None:
        """Name the recording folder "<date>_<title>" (empty — the start-time name, None — the
        window was closed); fix the paths of the last recording."""
        if name is None:
            return
        from ..live.setup import rename_session

        with FILES_LOCK:
            if record_id is not None:
                current = self._folder_of(record_id)
                if current is None:
                    return
                folder = current
            if str(folder.absolute()) in self._sync_busy():
                return  # being recorded or processed; a waiting one can be renamed
            try:
                new = rename_session(folder, name)
            except OSError:
                logger.warning("recording %s not renamed", folder, exc_info=True)
                self.state.error = _(
                    "Could not rename the recording. See the Mnema log for details"
                )
            else:
                self._follow(folder, new)
        self._scan_unfinished()  # paths in the list may point to the old folder name
        self._library_changed()

    def _follow(self, folder: Path, new: Path) -> None:
        """A recording folder was renamed or moved: notifications, the last recording and the
        "<server> is unavailable" card with "Retry" follow it."""
        from .state import folder_of, is_artifact

        _MOVED[folder] = new
        with self._lock:  # a waiting task follows its folder
            for task in self._tasks:
                if task.job.folder == folder:
                    task.job.folder = new
                    if task.prepared is not None:
                        task.prepared.base = new
        old, to = folder.absolute(), new.absolute()
        if (server := self.state.unreachable.pop(old, None)) is not None:
            self.state.unreachable[to] = server
        if (retry := self._retries.pop(old, None)) is not None:
            self._retries[to] = retry
        last = self.state.last_result
        if last is not None and folder_of(last) == folder:
            self.state.last_result = new / last.name if is_artifact(last) else new
            if (new / FileName.SUMMARY).exists():
                self.state.last_summary = new / FileName.SUMMARY

    def _ask_section(self, folder: Path, *, record_id: str | None = None) -> None:
        """The "New section…" item: a name window; the section is created, the recording moves
        in."""

        def show(done: Callable[[], None]) -> bool:
            def answered(name: str | None) -> None:
                try:
                    if name is not None:
                        self.move_folder(folder, name, record_id=record_id, create=True)
                finally:
                    done()

            self._platform.ask_section(answered)
            return True

        self._prompt(f"section:{record_id or folder}", show)

    def move_folder(
        self,
        folder: Path,
        section: str,
        *,
        record_id: str | None = None,
        create: bool = False,
    ) -> None:
        """The "Move to section" item (recordings.move_record; create — "New section…"). A recording
        being recorded, processed (here or by another process — the folder lock) or synced
        stays; a waiting one can move. A failure — the reason as a notice, the recording stays;
        fix the paths of the last recording and of the "<server> is unavailable" card."""
        from .recordings import move_record

        with FILES_LOCK:
            if record_id is not None:
                current = self._folder_of(record_id)
                if current is None:
                    self.state.error = _("Recording not found")
                    self._library_changed()
                    return
                folder = current
            if str(folder.absolute()) in self._sync_busy():
                return
            try:
                new = move_record(library_root(), folder, section, create=create)
            except ValueError:
                return  # nothing is left of the name: the windows never let that through
            except MediaError as e:  # another process is processing it
                self.state.error = str(e)
                self._library_changed()
                return
            except OSError:
                logger.warning("recording %s not moved to %r", folder, section, exc_info=True)
                self.state.error = _("Could not move the recording. See the Mnema log for details")
                self._library_changed()
                return
            if new != folder:
                self._follow(folder, new)
        self._scan_unfinished()
        self._library_changed()

    def _toggle_source(self, label: str) -> None:
        """Turn a source on or off mid-recording (a muted one writes silence)."""
        prepared = self._prepared
        w = prepared.writers.get(label) if prepared is not None else None
        if self.state.phase != Phase.RECORDING or w is None:
            return
        w.muted = not w.muted
        self.tick()

    # ── windows with a check and the model list ───────────────────────────────

    def _form(self, flow: Flow) -> None:
        """A form window: platform.open_form(flow.ask()); on an event — update(busy), the
        network in a worker, update(result) with the event's rev; a step without a check runs
        at once, without "Checking…"; other events are ignored while a check runs, and a result
        goes nowhere once the window is closed."""
        try:
            first = flow.ask()
        except Exception as e:  # noqa: BLE001 - the window did not open: the reason in the menu
            logger.warning("window did not open", exc_info=True)
            self.state.error = _error_text(e)
            self._on_change()
            return
        lock = threading.Lock()
        current, submitting = [first], [False]
        opened = threading.Event()  # the window may answer before open_form returns
        updates: list[FormUpdate] = []

        def send(ask: FormAsk | None) -> None:
            opened.wait(5.0)
            if updates:
                updates[0](ask)

        def work(ev: FormEvent) -> None:
            try:
                result = flow.submit(ev.values, force=ev.op == FormOp.FORCE)
            except Exception as e:  # noqa: BLE001 - the window stays open with the reason
                logger.warning("window %r: action failed", first.title, exc_info=True)
                update = {"error": _error_text(e), "busy": "", "force": "", "clear": []}
                result = current[0].model_copy(update=update)
            with lock:
                submitting[0] = False
                if flow.closed:
                    return
                if result is not None:
                    current[0] = result
            if result is None:
                send(None)
                self._form_saved(flow)
            else:
                send(result.model_copy(update={"rev": ev.rev}))

        def on_event(ev: FormEvent | None) -> None:
            with lock:
                if ev is None:
                    flow.closed = True
                    return
                if submitting[0]:
                    return
                submitting[0] = True
            if not flow.checks:  # a step without network: the window is not locked
                work(ev)
                return
            busy = {"busy": _("Checking…"), "rev": ev.rev, "error": "", "force": "", "clear": []}
            send(current[0].model_copy(update=busy))  # the whole window is locked while checking
            threading.Thread(target=work, args=(ev,), daemon=True, name="mnema-bar-form").start()

        updates.append(self._platform.open_form(first, on_event))
        opened.set()

    def _form_saved(self, flow: Flow) -> None:
        """The window saved. A model or a key — the settings rows are reread, from the server
        when it keeps them, and a recognition key changes the cloud list; a connection — check
        the server, wake the sync, reread the recognition list."""
        self.state.error = ""
        if not isinstance(flow, ServerFlow):
            self._env_changed()
            if self.server is not None:
                self._check_server_async(status=False)
            if isinstance(flow, KeyFlow):
                self._load_asr_choices()
            return
        if flow.result is not None:
            self.server_ok, self.server_auth = flow.result
            self.server_name, self.server_llm, self.server_model = None, False, None
            self.server_version = None
            self.server_asr_key = self.server_deepl = False
        self._env_changed()
        self._check_server_async(status=False)
        self._load_asr_choices()
        self._library_changed()

    def _set_recognition(self, value: str) -> bool:
        """ "Recognition": On this Mac (Mac only), On the server (needs a connected server) or In
        the cloud; the engine and its list become the place's. False — the choice stays."""
        if value not in set(Recognition) or (value == Recognition.MAC and not _base.STANDALONE):
            return False
        if value == Recognition.SERVER and self.server is None and _base.STANDALONE:
            return False
        self.prefs = BarPrefs.model_validate({**self.prefs.model_dump(), "recognition": value})
        self._load_asr_choices()
        self._check_server_async()  # name, model and reachability of the server for "Model"
        self._env_changed()
        return True

    def _ask_server_off(self) -> None:
        """The "Disconnect" button asks first: connecting again takes a new invitation."""
        server = self.server
        if server is None:
            return

        def answered(yes: bool) -> None:
            # another server was connected or recording started during the question: hands off
            if yes and self.server == server and self.state.phase == Phase.IDLE:
                self._server_off()

        label = server_label(server, self.server_name)
        # the default name as stored or as an English server reports it: not "the server “Server”"
        if label in (SERVER_NAME, "Server"):
            title = _("Disconnect from the server?")
        else:
            title = _("Disconnect from the server “{name}”?", name=label)
        if _base.STANDALONE:
            message = _(
                "Recordings stay on this Mac and on the server. "
                "To connect again, you need a new invitation from the server"
            )
        else:
            message = _(
                "Recordings stay on this computer and on the server. "
                "To connect again, you need a new invitation from the server"
            )
        self._platform.confirm(title, message, _("Disconnect"), answered)

    def _server_off(self) -> None:
        """Disconnect: empty address and token in ~/.config/mnema/.env; sync.json stays."""
        from ..config import set_env

        try:
            set_env({"MNEMA_RUNNER": "", "MNEMA_TOKEN": ""})
        except OSError as e:
            self.state.error = _(
                "Could not save the server settings: {error}", error=e.strerror or e
            )
            self._on_change()
            return
        self.state.error = ""
        if _base.STANDALONE and self.prefs.recognition == Recognition.SERVER:
            # without a server the Mac recognizes itself
            update = {"recognition": Recognition.MAC}
            self.prefs = BarPrefs.model_validate({**self.prefs.model_dump(), **update})
            self._save_prefs()
        self._load_asr_choices()  # the recognition list came from the server
        self.server_ok, self.server_auth = False, True
        self.server_name, self.server_llm, self.server_model = None, False, None
        self.server_version = None
        self.server_asr_key = self.server_deepl = False
        self.state.sync_missing, self.state.sync_errors = [], []
        self.state.sync_other_library, self.state.sync_foreign = None, ""
        self._library_changed()
        self._env_changed()

    def close(self) -> None:
        """The app quits: stop the sync."""
        if self._sync is not None:
            self._sync.close()

    def _processing_label(self) -> str:
        """The server by name, as in the settings; without a name — by address."""
        remote = self.summary_connection()
        return server_label(remote.url if remote else None, self.server_name)

    def _server_down(self, folder: Path, retry: Callable[..., None], message: str) -> None:
        """The chosen server did not answer: the recording card shows "<server> is
        unavailable" with "Retry" (retry(record_id=…), the same processing) and "Choose
        server". It neither retries nor switches by itself."""
        server = self._processing_label()
        with self._lock:
            self.state.unreachable[folder.absolute()] = server
            self._retries[folder.absolute()] = retry
        self._platform.notify(_("{server} is unavailable", server=server), message, folder)

    def _retry(self, folder: Path, record_id: str) -> None:
        """The "Retry" button: the same processing on the server chosen now."""
        if (retry := self._retries.get(folder.absolute())) is not None:
            retry(record_id=record_id)

    # ── processing queue ───────────────────────────────────────────────────────

    def _sync_jobs(self) -> None:
        """The queue as the panel and the menu see it; under self._lock."""
        self.state.jobs = [t.job for t in self._tasks]

    def _folder_of(self, record_id: str) -> Path | None:
        """A recording folder by id: in the queue (possibly outside the library), else in the
        library."""
        task = self._task(record_id)
        if task is not None and task.job.folder.is_dir():
            return task.job.folder
        return find_recording(library_root(), record_id)

    def _enqueue(self, task: _Task) -> bool:
        """To the end of the queue; start the processing thread if it is idle. The same
        recording already queued — a notice."""
        with self._lock:
            if self.quit_requested:
                return False
            if any(t.job.id == task.job.id for t in self._tasks):
                self.state.notice = _(QUEUED_ALREADY)
                queued = False
            else:
                self.state.unreachable.pop(task.job.folder.absolute(), None)  # it goes again
                self._retries.pop(task.job.folder.absolute(), None)
                self._tasks.append(task)
                self._sync_jobs()
                queued = True
                if self._worker is None:
                    self._worker = threading.Thread(
                        target=self._work, daemon=True, name="mnema-bar-process"
                    )
                    self._worker.start()
        if queued:  # queued: no longer "Not processed"
            self._scan_unfinished()
        self._on_change()
        return queued

    def _identity(self, folder: Path) -> str:
        """The recording's meta.id for a task: it finds the folder after a rename or a move."""
        from ..sync.scan import ensure_identity

        try:
            return ensure_identity(folder)
        except Exception:  # noqa: BLE001 - meta is not writable: the task finds it by path
            logger.warning("id of recording %s not written", folder, exc_info=True)
            return str(folder.absolute())

    def summarize(self, folder: Path, *, record_id: str | None = None) -> None:
        """The card's "Summarize" goes to the queue: setup.summarize_folder where processing
        runs, remarks in the recording history, a notification about the result."""
        with FILES_LOCK:
            if record_id is not None:
                current = self._folder_of(record_id)
                if current is None:
                    return
                folder = current
            if self._is_recording(folder):
                self.state.notice = _(STILL_RECORDING)
                self._on_change()
                return
            if self.quit_requested or not folder.is_dir():
                return
            job = Job(id=self._identity(folder), folder=folder, kind=JobKind.SUMMARIZE)
        self._enqueue(_Task(job))

    def _is_recording(self, folder: Path) -> bool:
        active = self.active_folder
        return active is not None and folder.resolve() == active.resolve()

    def _ask_range_for(self, folder: Path | None, *, record_id: str | None = None) -> None:
        """A folder was picked: the "Process recording" window — which part (the whole at
        first), the title (the folder's own one filled in; empty — by content), the language
        (by speech at first; a chosen one is strict), full recording to the Trash (as last
        time)."""
        if folder is None:
            return
        if self._is_recording(folder):  # no part window over a WAV still being written
            self.state.notice = _(STILL_RECORDING)
            self._on_change()
            return
        from ..live.setup import (
            fmt_clock,
            is_recording_folder,
            parse_offset,
            recording_length,
        )
        from .range_pick import RangeRequest
        from .range_pick import whole as whole_part
        from .state import result_name, title_of

        length = recording_length(folder)
        if not length:
            self.state.error = _("The folder “{name}” has no WAV audio", name=folder.name[:30])
            self._on_change()
            return
        sources = _sources(folder)
        named = title_of(folder) or ""
        current = self._own_title(folder) or named  # the own title of an interrupted run
        req = RangeRequest(
            length=length,
            subtitle=f"{result_name(folder)} · {fmt_clock(length)}",
            name=current,
            replace=self.prefs.replace_original,
            can_replace=is_recording_folder(folder)
            and folder.resolve() != library_root().resolve(),
            asr=self.prefs.asr,
            asrs=asr_options(self.menu_env(), self.prefs.asr),
            sources=sources,
        )

        def got(answer: RangeAnswer | None) -> None:
            if answer is None:
                return
            tracks = None  # the window did not ask: all tracks
            if answer.sources is not None and sources:
                tracks = [s.label for s in sources if s.label in answer.sources]
                if not tracks:
                    self.state.error = _("No source selected")
                    self._on_change()
                    return
            try:
                a = parse_offset(answer.start) if answer.start.strip() else 0.0
                b = parse_offset(answer.end) if answer.end.strip() else length
            except Exception as e:  # noqa: BLE001 - a MediaError with a clear text goes to the menu
                self.state.error = str(e)
                self._on_change()
                return
            if b <= a:
                self.state.error = _(
                    "End {end} is before start {start}", end=fmt_clock(b), start=fmt_clock(a)
                )
                self._on_change()
                return
            self.state.error = ""
            whole = whole_part(a, b, length)
            replace = answer.replace and req.can_replace and not whole
            if not whole and req.can_replace and answer.replace != self.prefs.replace_original:
                self.prefs.replace_original = answer.replace  # remembered for next time
                self._save_prefs()
            title = answer.title.strip()
            lang = answer.lang if answer.lang in dict(LANGS) and answer.lang != "auto" else None
            self.recover(
                folder,
                start=None if whole else a,
                end=None if whole else b,
                title=title if title != named else None,
                auto_name=not title,
                replace=replace,
                lang=lang,
                asr=answer.asr or None,
                tracks=tracks,
                record_id=record_id,
            )

        self._platform.ask_range(req, got)

    def recover(
        self,
        base: Path,
        *,
        start: float | None = None,
        end: float | None = None,
        title: Any = None,
        auto_name: bool | None = None,
        replace: bool = False,
        lang: str | None = None,
        asr: str | None = None,
        tracks: list[str] | None = None,
        record_id: str | None = None,
    ) -> None:
        """Process a recording again, through the queue: an interrupted or stopped one, or only
        a part of it. title — own title (... — as in the recording's work file: "Retry");
        auto_name — name by content (None — only instead of a default name); replace — the full
        recording to the Trash after a part is processed; lang — strict language (None — by
        speech); asr — recognition for this recording (None — the setting when processing
        starts); tracks — which tracks to transcribe (None — all)."""
        with FILES_LOCK:
            if record_id is not None:
                current = self._folder_of(record_id)
                if current is None:
                    return
                base = current
            if self._is_recording(base):
                self.state.notice = _(STILL_RECORDING)
                self._on_change()
                return
            if self.quit_requested or not base.is_dir():
                return
            job = Job(id=self._identity(base), folder=base, kind=JobKind.RECOVER, lang=lang)
        part = _Part(start=start, end=end, title=title, auto_name=auto_name, replace=replace)
        self._enqueue(_Task(job, part=part, asr=asr, tracks=tracks))

    def _work(self) -> None:
        """The "mnema-bar-process" thread: queue tasks one by one, first in first out; empty —
        exit (a new task starts the thread again)."""
        while True:
            completed: Path | None = None
            # take a task, find its folder, create the work file and become "running" under
            # one FILES_LOCK: renaming, moving and sync cannot cut in between, and afterwards
            # the folder is busy (running)
            with FILES_LOCK:
                with self._lock:
                    if not self._tasks or self.quit_requested:
                        self._worker = None
                        return
                    task = self._tasks[0]
                    task.taken = True
                try:
                    folder = self._begin(task)
                except Exception:  # noqa: BLE001 - one failed task does not stop the queue
                    logger.exception("processing of %s did not start", task.job.folder)
                    folder = None
                if folder is not None:
                    with self._lock:
                        task.job.running = True
                        task.since, task.began = time.monotonic(), meta.now()
                        self._sync_jobs()
            try:
                if folder is not None:
                    self._on_change()
                    completed = self._run_task(task, folder)
            except Exception:  # noqa: BLE001 - one failed task does not stop the queue
                logger.exception("processing of %s failed", task.job.folder)
            finally:
                with self._lock:
                    if task in self._tasks:
                        self._tasks.remove(task)
                    self._sync_jobs()
                self._scan_unfinished()
                self._library_changed()
            try:
                self._after(task, completed)
            except Exception:  # noqa: BLE001
                logger.exception("after processing %s", task.job.folder)

    def _begin(self, task: _Task) -> Path | None:
        """The start of a task: the folder by id (it may have been renamed or moved while
        waiting; gone — the task is dropped quietly); processing gets a work file so an own
        title can be set from the first second."""
        from ..live.recording_client import begin, set_own_title

        folder = task.job.folder
        if not folder.is_dir():
            found = find_recording(library_root(), task.job.id)
            if found is None:
                logger.info("recording %s vanished before processing", task.job.id)
                return None
            folder = task.job.folder = found
        if task.prepared is not None:
            task.prepared.base = folder
        part = task.part
        if task.job.kind is JobKind.SUMMARIZE or part.start is not None or part.end is not None:
            return folder  # a part is a folder of its own, processing creates its work file
        try:
            if task.prepared is not None or part.title is ...:
                begin(folder)  # "Set title…" during processing replaces the invented one
            else:
                set_own_title(folder, part.title, create=True)
        except (OSError, MediaError) as e:
            self.state.error = _("Could not start processing: {error}", error=e)
            return None
        return folder

    def _run_task(self, task: _Task, folder: Path) -> Path | None:
        """A task takes the processing settings when it starts, not when it is queued."""
        if task.job.kind is JobKind.SUMMARIZE:
            self._summarize(task, folder)
            return None
        return self._process(task, folder)

    def _after(self, task: _Task, completed: Path | None) -> None:
        """An own title given without a work file goes to the folder; after a recording — the
        title window, unless recording is under way or the user gave a title."""
        if self._name_late(task, completed) or completed is None:
            return
        if task.job.kind is JobKind.RECORD:
            self._ask_result_title(task, completed)

    def _summarize(self, task: _Task, folder: Path) -> None:
        from ..live.recording_client import drop_request, summarize_on_node
        from .state import readable_notes, result_name

        job = task.job
        job.stage, job.progress = SUMMARIZING, None

        def on(_frac: float | None, _msg: str) -> None:
            if task.stop:  # "Stop processing": the folder stays as it was
                raise FinalizeCancelled

        try:
            from ..client import MnemaClient

            connection = self.summary_connection()
            if connection is None and not _base.STANDALONE:
                self.state.error = _("Server not connected")
                return
            if (
                connection is not None
                and MnemaClient(connection.url, token=connection.token).health() is None
            ):
                # no waiting for an upload into silence: "<server> is unavailable" at once
                self._server_down(folder, partial(self.summarize, folder), _("Summary not written"))
                return
            result = summarize_on_node(
                folder,
                connection,
                lang=self.prefs.summary_lang,
                on=on,
                on_base=partial(self._moved, task),
            )
            base = result.base or job.folder
            if base != folder:
                _MOVED[folder] = base
            self.state.last_result = result.transcript_txt or base / "transcript.txt"
            self.state.last_warnings = list(result.remarks)
            self.state.last_summary = result.summary_md
            if result.summary_md is not None:
                self._platform.notify(_("Summary ready"), result_name(base), result.summary_md)
            else:
                reason = next(iter(readable_notes(result.remarks)), "")
                self._platform.notify(_("Summary not written"), reason, base)
        except FinalizeCancelled as e:
            # a stopped summary leaves the history alone, as on the phone: the recording as is
            drop_request(getattr(e, "mnema_base", None) or job.folder)
        except Exception as e:  # noqa: BLE001 - the error in the menu, the folder untouched
            logger.exception("summary of %s not written", folder)
            self.state.error = _failure_text(
                e, _("Could not write the summary. See the Mnema log for details")
            )

    def _process(self, task: _Task, base: Path) -> Path | None:
        """Process a recording: a just-finished one (finalize_on_node) or "Process…"
        (recover_on_node). The language was corrected — interrupt and start over strictly in
        it; stopped — the audio stays. Returns the result for the title window (None — none)."""
        from ..live.recording_client import finalize_on_node, recover_on_node
        from ..live.setup import is_default_name

        job, part = task.job, task.part
        connection = self.processing_connection()
        whole = part.start is None and part.end is None
        auto_name = part.auto_name
        if auto_name is None:  # by the original name, also on a restart after a rename
            auto_name = is_default_name(base)
        fresh = task.prepared is not None  # just recorded
        # tracks from the "Process recording" window (a just-finished recording uses all)
        chosen = task.tracks if job.kind is JobKind.RECOVER else None
        try:
            while True:
                try:
                    if fresh:
                        prepared = task.prepared
                        assert prepared is not None
                        # the settings as of the start of processing, not of recording
                        prepared.config = prepared.config.model_copy(
                            update=self._processing_settings(connection)
                        )
                        result, _full = finalize_on_node(
                            prepared,
                            task.tracks or [],
                            connection=connection,
                            on=partial(self._on_progress, task),
                            on_transcript=partial(self._transcript_ready, task),
                            on_lang=partial(self._on_lang, task),
                            on_base=partial(self._moved, task),
                        )
                        done = prepared.base = result.base or prepared.base
                        retry = partial(  # the own title is in the recording's work file
                            self.recover, done, title=..., lang=job.lang,
                            asr=prepared.config.asr,
                        )  # fmt: skip
                    else:
                        asr = asr_at(task.asr or self.prefs.asr, self._place())
                        config = self._config().model_copy(update={"lang": job.lang, "asr": asr})
                        result, _full = recover_on_node(
                            base,
                            config,
                            connection,
                            start=part.start,
                            end=part.end,
                            retitle=auto_name,
                            replace_original=self._platform.trash if part.replace else None,
                            title=... if whole else part.title,
                            on_transcript=partial(self._transcript_ready, task),
                            on_lang=partial(self._on_lang, task),
                            on_base=partial(self._moved, task),
                            on=partial(self._on_progress, task),
                            tracks=chosen,
                        )
                        # a part is a folder of its own, the full one may have gone
                        done = result.base or base
                        retry = partial(
                            self.recover, base, start=part.start, end=part.end,
                            title=... if whole else part.title, auto_name=auto_name,
                            replace=part.replace, lang=job.lang, asr=task.asr, tracks=chosen,
                        )  # fmt: skip
                    self._finish(result, done, retry)
                    # never reached the server: no part, so the full one gets no part title
                    return None if result.defer_command else self.state.last_result
                except FinalizeCancelled as e:
                    folder = getattr(e, "mnema_base", None) or job.folder  # may be renamed
                    if not self._restarting(task):
                        self._audio_only(task, folder)
                        return None
                    if whole:
                        base = job.folder = folder
                    elif folder != base:  # the part is cut from the full one again: drop it
                        shutil.rmtree(folder, ignore_errors=True)
                    if fresh:  # language corrected: start over strictly in it, name by content
                        fresh, auto_name = False, True
                        task.prepared = None
        except Exception as e:  # noqa: BLE001 - the error in the menu, the recording untouched
            logger.exception("processing of %s failed", base)
            folder = getattr(e, "mnema_base", None) or job.folder
            self.state.error = _failure_text(
                e, _("Could not process the recording. See the Mnema log for details")
            )
            if job.kind is JobKind.RECORD and any(folder.glob("*.wav")):
                self.state.last_result = folder  # the recording is intact: it can be opened
            return None

    def _processing_settings(self, connection: Any) -> dict[str, Any]:
        """Recognition and summary as chosen now, where processing runs."""
        return {
            "asr": asr_at(self.prefs.asr, self._place()),
            "summary": self.prefs.summary,
            "summary_lang": self.prefs.summary_lang,
            "runner": connection.url if connection is not None else None,
        }

    def _name_late(self, task: _Task, completed: Path | None) -> bool:
        """An own title given while there was no work file names the ready folder."""
        name, task.late_title = task.late_title, None
        if name is None or completed is None:
            return False
        from .state import folder_of

        self.rename_folder(followed(folder_of(completed)), name)
        return True

    def _moved(self, task: _Task, folder: Path) -> None:
        """Processing renamed the folder: notifications, the transcript and the card follow
        it; sync sees the rename and the busy folder's path in one step."""
        job = task.job
        with FILES_LOCK:
            previous = job.folder
            if previous != folder and not previous.exists():
                _MOVED[previous] = folder
            ready = job.transcript_ready
            if ready is not None and ready.parent != folder:
                _MOVED[ready.parent] = folder
                job.transcript_ready = folder / ready.name
            job.folder = folder
            if task.prepared is not None:
                task.prepared.base = folder
        self.library_rev += 1
        self._on_change()

    def _transcript_ready(self, task: _Task, path: Path) -> None:
        """Transcribed: the transcript can be read now, the summary comes later. One
        notification, about the first text."""
        first = task.job.transcript_ready is None
        task.job.transcript_ready = path
        if first:
            self._platform.notify(_("Transcript ready"), result_name(path), path)
        self._on_change()

    def _on_progress(self, task: _Task, frac: float | None, msg: str) -> None:
        """Processing progress → the card and the menu; "Stop processing" or another language
        interrupts the step here."""
        if task.stop:
            raise FinalizeCancelled
        job = task.job
        if frac is not None:
            job.progress = max(job.progress or 0.0, min(frac, 1.0))
        if msg in STEP_AT:  # a step starts: its caption in the card and the menu
            job.stage = msg
        self._on_change()

    def _on_lang(self, task: _Task, languages: dict[str, str]) -> None:
        """Track languages are decided: the card shows the one applied to the recording."""
        task.job.languages = dict(languages)
        self._on_change()

    def _relang(self, task: _Task, lang: str) -> None:
        """The language was corrected during processing: interrupt it and start over strictly
        in it (the audio stays); every track already has this language — carry on."""
        job = task.job
        applied = set(job.languages.values()) or {job.lang}
        if lang not in dict(LANGS) or lang == "auto" or applied == {lang}:
            return
        job.lang, job.languages = lang, {}
        task.restart = task.stop = True

    def _restarting(self, task: _Task) -> bool:
        """Processing was interrupted by a language change: free the server step, start over."""
        if not task.restart:
            return False
        from ..client import cancel_active_job

        cancel_active_job()
        job = task.job
        task.stop = task.restart = False
        job.transcript_ready, job.progress, job.stage, job.languages = None, 0.0, "", {}
        job.elapsed, task.since, task.began = 0.0, time.monotonic(), meta.now()
        self._on_change()
        return True

    def _audio_only(self, task: _Task, base: Path) -> None:
        """Processing was stopped: the audio stays, no text — show the folder, no error."""
        from ..client import cancel_active_job
        from ..live.setup import keep_audio_only

        cancel_active_job()  # a step was running on the server: free it
        keep_audio_only(base, started=task.began)
        self.state.last_result = base
        self.state.last_warnings = []
        self.state.last_summary = None
        self._platform.notify(
            _("Processing stopped"), _("The audio is saved; you can process it later"), base
        )

    def _finish(self, result: Any, base: Path, retry: Callable[..., None]) -> None:
        """The processing result: the last recording, remarks, a notification. The chosen
        server did not answer — the recording waits for it: "Retry" is retry, the same
        processing."""
        from ..live import remarks

        if result.defer_command:  # the remark names the server as in the settings, no command
            down = remarks.server_down(self._processing_label())
            result.remarks = [down if r.step == remarks.Step.SERVER else r for r in result.remarks]
        self.state.last_warnings = list(result.remarks)
        self.state.last_summary = result.summary_md
        if result.defer_command:
            self.state.last_result = base
            self._server_down(base, retry, _("The recording is saved; process it later"))
        elif result.transcript_txt is not None:
            self.state.last_result = result.transcript_txt
            name = result_name(result.transcript_txt)
            message = _("{name}, with notes", name=name) if result.refine_warnings else name
            partial = any(r.step == "record" for r in result.remarks)
            heading = _("Incomplete recording processed") if partial else _("Recording processed")
            self._platform.notify(heading, message, result.transcript_txt)
        else:
            self.state.last_result = base
            self._platform.notify(
                _("Recording saved without a transcript"), result_name(base), base
            )

    def stop(self, *, cancel: bool = False) -> None:
        with self._lock:
            if self.state.phase != Phase.RECORDING:
                return
            self._stop_requested = True
            self._cancel = cancel
            session = self._session
        if session is not None:
            session.stop()
        self._on_change()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the recording and the processing queue behind it."""
        end = None if timeout is None else time.monotonic() + timeout

        def left() -> float | None:
            return None if end is None else max(0.0, end - time.monotonic())

        if (t := self._thread) is not None:
            t.join(left())
        while (w := self._worker) is not None and w.is_alive():
            w.join(left())
            if end is not None and time.monotonic() >= end:
                return

    # ── quit ───────────────────────────────────────────────────────────────────

    def _ask_quit(self) -> None:
        """Quitting Mnema: nothing to interrupt — at once; recording or processing — a question."""
        recording, processing = self.state.phase == Phase.RECORDING, bool(self._tasks)
        if not (recording or processing):
            self._quit_now()
            return
        if recording and processing:
            message = _(QUIT_BOTH)
        elif recording:
            message = _(QUIT_RECORDING)
        else:
            message = _(QUIT_PROCESSING)
        self._platform.confirm(
            _(QUIT_TITLE),
            message,
            _("Quit"),
            lambda yes: self._quit_now() if yes else None,
            cancel=_("Cancel"),
        )

    def _quit_now(self) -> None:
        """Quit: stop and save the recording without processing, drop the queue (it is not
        kept between launches); quitting interrupts the running processing with no history
        entry, the recording stays "Not processed", "Process…" continues the server step with
        the same job."""
        with self._lock:
            self.quit_requested = True
            self._tasks = [t for t in self._tasks if t.job.running]
            self._sync_jobs()
        if self.state.phase == Phase.RECORDING:
            self.stop()
        self._on_change()

    def _config(self) -> Any:
        from ..live.setup import LiveConfig

        remote = self.processing_connection()
        runner = remote.url if remote else None
        # BOTH sources are opened (system audio if available): one turned off in the settings
        # writes silence and can be turned on from the menu mid-recording ("forgot at start");
        # no language is set: the core detects the call's language from the voice
        return LiveConfig(
            mic=True,
            system=self.prefs.system or _system_available(),
            asr=asr_at(self.prefs.asr, self._place()),
            summary=self.prefs.summary,
            summary_lang=self.prefs.summary_lang,
            stream=False,  # recording only: the text comes from an offline pass after stop
            runner=runner,
        )

    def _record(self, config: Any) -> tuple[Any, Any]:
        """Build and run a recording (blocks until stop). The folder of a recording that never
        happened (a source did not open) is removed, or it would hang as "not processed"."""
        from ..live.setup import build_live, live_base

        with FILES_LOCK:
            base = live_base(None, section=self._record_section())
            self._active = base
        prepared = build_live(config, base=base)
        for label, w in prepared.writers.items():  # sources off in the settings write silence
            w.muted = not (self.prefs.mic if label == "mic" else self.prefs.system)
        try:
            session = prepared.make_session(None)
            with FILES_LOCK, self._lock:
                self._prepared, self._session = prepared, session
                if self._stop_requested:  # stop came while the session was being built
                    session.stop()
            self._on_change()
            tracks = session.run()
            prepared.recording_error = getattr(session, "recording_error", None)
            if prepared.recording_error:
                self.state.notice = _(
                    "The recording is incomplete: {error}. What was recorded is saved",
                    error=prepared.recording_error,
                )
                self._platform.notify(_("Recording stopped"), self.state.notice, prepared.base)
        except BaseException as e:
            if isinstance(e, MicUnavailable):
                with FILES_LOCK, self._lock:
                    self._prepared = self._session = None
                shutil.rmtree(prepared.base, ignore_errors=True)
            raise
        return prepared, _drop_unused_tracks(prepared, tracks)

    def _startup_notice(self, connection: Any) -> str:
        """No server, or it is unavailable: only the audio is saved, it can be processed later;
        a Mac processes itself."""
        from ..client import MnemaClient

        if connection is None:
            return (
                "" if _base.STANDALONE else _("Server not connected, only the audio will be saved")
            )
        status = MnemaClient(connection.url, token=connection.token).health()
        if status is None:
            return _(
                "{server} is unavailable, only the audio will be saved",
                server=self._processing_label(),
            )
        if status.get("auth") is False:
            return _("A new invitation from the server is needed, only the audio will be saved")
        return ""

    def _run(self) -> None:
        """The "mnema-bar-rec" thread only records. On stop the tracks are closed, the panel
        shows "Start recording" again and the recording goes to the end of the queue."""
        prepared = None
        try:
            config = self._config()
            connection = self.processing_connection()
            self.state.notice = self._startup_notice(connection)
            try:
                prepared, tracks = self._record(config)
            except MicUnavailable:
                if not config.system:
                    raise
                # the microphone did not open but system audio is there: record it, do not fail
                self.state.notice = _("Microphone unavailable, recording system audio only")
                prepared, tracks = self._record(config.model_copy(update={"mic": False}))
            if self._cancel:
                shutil.rmtree(prepared.base, ignore_errors=True)
                self._platform.notify(
                    _("Recording deleted"), _("The audio and the transcript were not saved"), None
                )
                return
            if not self.quit_requested:  # quitting: the recording is saved, process it later
                with FILES_LOCK:
                    job = Job(id=self._identity(prepared.base), folder=prepared.base)
                self._enqueue(_Task(job, prepared=prepared, tracks=tracks))
        except Exception as e:  # noqa: BLE001 - background worker: the error in the menu
            logger.exception("menu bar recording failed")
            prepared = prepared or self._prepared
            recorded = prepared is not None and any(
                getattr(w, "written", 0) for w in prepared.writers.values()
            )
            failed = (
                _("The recording stopped because of an error. See the Mnema log for details")
                if recorded
                else _("Could not start recording. See the Mnema log for details")
            )
            self.state.error = _failure_text(e, failed)
            if prepared is not None and any(w.path.exists() for w in prepared.writers.values()):
                self.state.last_result = prepared.base  # the recording is intact: it can be opened
        finally:
            with FILES_LOCK, self._lock:
                self._prepared = self._session = None
                self._active = None
            self.state.phase = Phase.IDLE
            self.state.meters = []
            self.state.paused = False
            self._scan_unfinished()
            self._library_changed()

    def _ask_result_title(self, task: _Task, path: Path) -> None:
        """The title window after processing ("Skip"): only when nothing is being recorded and
        the user gave no title; while recording, the card can rename it."""
        from .state import folder_of, title_of

        folder = folder_of(path)

        def wanted() -> bool:  # the window waited for another one: is it still needed
            return not (
                self.quit_requested or task.named_by_user or self.state.phase == Phase.RECORDING
            )

        if not wanted():
            return

        def current() -> str | None:
            return title_of(followed(folder))

        def named(value: str | None) -> None:
            name = (value or "").strip()
            if name and name != current():
                self.rename_folder(followed(folder), name)

        self._ask_title(f"result:{task.job.id}", current, named, cancel=_("Skip"), when=wanted)

    # ── GUI timer ──────────────────────────────────────────────────────────────

    def tick(self) -> None:
        """Every ~0.5 s from the GUI timer: the recording timer and source meters; how long
        processing runs (the menu's "N min" shows it is alive); when idle, a 12-hourly update
        check; the server does not answer — ask it again every 30 s."""
        # due, or the clock was set back more than an interval
        wait = self._update_next - time.time()
        if self.state.phase == Phase.IDLE and not 0 < wait <= _UPDATE_EVERY:
            self._check_updates()
        now = time.monotonic()
        if not self.server_ok and self.server_auth and now - self._ping_at > _REPING_EVERY:
            self._ping_at = now  # no server: .env is not reread on every tick either
            if self.server is not None:
                self._check_server_async()  # the Mac may have woken before the network
        with self._lock:
            running = next((t for t in self._tasks if t.job.running), None)
        if running is not None and running.since is not None:
            running.job.elapsed = now - running.since
        if self.state.phase != Phase.RECORDING:
            return
        paused_now = now - self._paused_at if self._paused_at is not None else 0.0
        self.state.elapsed = now - self._started - self._paused_total - paused_now
        prepared = self._prepared
        if prepared is None:
            return
        if any(getattr(w, "full", False) for w in prepared.writers.values()):
            # a track reached the WAV limit (~37 h): stop, what was recorded is intact
            self.state.notice = _("Recording stopped: it cannot be longer than 37 hours")
            self.stop()
            return
        meters: list[SourceMeter] = []
        old = {m.label: m for m in self.state.meters}
        for label, w in prepared.writers.items():
            prev = old.get(label) or SourceMeter(label=label)
            history = (prev.history + [float(getattr(w, "level", 0.0))])[-_HISTORY:]
            muted = bool(getattr(w, "muted", False))
            meters.append(SourceMeter(label=label, history=history, muted=muted))
        self.state.meters = meters
