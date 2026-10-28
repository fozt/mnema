"""mnema-bar for Linux (Ubuntu GNOME): an icon in the top bar through pystray and AppIndicator.

The menu comes from state.menu, the icon is a picture (icons.tray_icon), the recording timer is
the label next to the icon (AppIndicator label). Notifications — notify-send with an Open action;
files — xdg-open, show in folder — org.freedesktop.FileManager1. GNOME needs the AppIndicator
extension (enabled in Ubuntu).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..i18n import _
from .runtime import BarRuntime, followed
from .state import MenuItem, Phase, menu, title

_TICK_SECONDS = 0.5


class LinuxPlatform:
    def __init__(
        self, *, on_form_change: Any = None, settings_parent: Any = None, on_settings: Any = None
    ) -> None:
        self._on_form_change = on_form_change
        self._settings_parent = settings_parent
        self._on_settings = on_settings  # opens the settings window on a tab

    def open(self, path: Path) -> None:
        subprocess.Popen(
            ["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def ask_title(self, current: str | None, on_done: Any, *, cancel: str | None = None) -> None:
        """The recording title window through zenity (shipped with Ubuntu), in its own thread so
        the menu does not wait."""

        def ask() -> None:
            cmd = [
                "zenity", "--entry", f"--title={_('Recording title')}", f"--text={_('Title')}",
                f"--entry-text={current or ''}", f"--ok-label={_('Save')}",
                f"--cancel-label={cancel or _('Cancel')}",
            ]  # fmt: skip
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            except Exception:  # noqa: BLE001 - no zenity: keep the current title
                on_done(None)
                return
            on_done(out.stdout.strip() if out.returncode == 0 else None)

        threading.Thread(target=ask, daemon=True, name="mnema-bar-title").start()

    def ask_section(self, on_done: Any) -> None:
        """The new section window through zenity. zenity cannot disable Create, so a name that
        leaves nothing for a folder name (naming.folder_title) is asked again, like the disabled
        Create button on the Mac."""
        from ..live.naming import folder_title
        from .protocol import NEW_SECTION_ASK as ask

        def run() -> None:
            typed = ""
            while True:
                cmd = [
                    "zenity", "--entry", f"--title={ask.title}", f"--text={ask.label}",
                    f"--entry-text={typed}", f"--ok-label={ask.ok}",
                    f"--cancel-label={ask.cancel}",
                ]  # fmt: skip
                try:
                    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                except Exception:  # noqa: BLE001 - no zenity: no section
                    on_done(None)
                    return
                if out.returncode != 0:  # Cancel, or the window was closed
                    on_done(None)
                    return
                typed = out.stdout.strip()
                if folder_title(typed):
                    on_done(typed)
                    return

        threading.Thread(target=run, daemon=True, name="mnema-bar-section").start()

    def choose_folder(self, start: Path, on_done: Any) -> None:
        """Pick a recording folder through zenity, in its own thread so the menu does not wait."""

        def ask() -> None:
            cmd = [
                "zenity", "--file-selection", "--directory",
                f"--title={_('Choose the recording folder')}", f"--filename={start}/",
            ]  # fmt: skip
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            except Exception:  # noqa: BLE001 - no zenity: nothing chosen
                on_done(None)
                return
            path = out.stdout.strip()
            on_done(Path(path) if out.returncode == 0 and path else None)

        threading.Thread(target=ask, daemon=True, name="mnema-bar-folder").start()

    def ask_range(self, req: Any, on_done: Any) -> None:
        """The process recording window: GTK (range_pick in its own process); without GTK, zenity
        fields one by one (start, end, title, sources), and the Trash checkbox as last time."""
        from ..live.setup import fmt_clock
        from .range_pick import CANCELLED, SOURCES_LABEL, TITLE, RangeAnswer, unasked_sources

        def zenity(text: str, value: str) -> str | None:
            cmd = [
                "zenity", "--entry", f"--title={TITLE}", f"--text={text}",
                f"--entry-text={value}", f"--ok-label={_('Continue')}",
                f"--cancel-label={_('Cancel')}",
            ]  # fmt: skip
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            return out.stdout.strip() if out.returncode == 0 else None

        def sources() -> list[str] | None:
            """Tracks as checkboxes; None when none is checked or the window is cancelled."""
            if len(req.sources) < 2:
                return unasked_sources(req)
            rows = [v for s in req.sources for v in ("TRUE" if s.on else "FALSE", s.label, s.title)]
            cmd = [
                "zenity", "--list", "--checklist", f"--title={TITLE}",
                f"--text={SOURCES_LABEL}", "--column=", "--column=",
                f"--column={SOURCES_LABEL}", "--hide-column=2",
                "--print-column=2", "--separator=,", "--hide-header",
                f"--ok-label={_('Process')}", f"--cancel-label={_('Cancel')}", *rows,
            ]  # fmt: skip
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            picked = [x for x in out.stdout.strip().split(",") if x] if out.returncode == 0 else []
            return [s.label for s in req.sources if s.label in picked] or None

        def fallback() -> RangeAnswer | None:
            start = zenity(req.subtitle + "\n" + _("Start"), "0:00:00")
            end = zenity(_("End"), fmt_clock(req.length)) if start is not None else None
            title = zenity(_("Title"), req.name) if end is not None else None
            if start is None or end is None or title is None:
                return None
            chosen = sources()
            if req.sources and chosen is None:
                return None
            return RangeAnswer(
                start=start,
                end=end,
                title=title,
                replace=req.replace and req.can_replace,
                sources=chosen,
            )

        def ask() -> None:
            cmd = [sys.executable, "-m", "mnema.bar.range_pick", req.model_dump_json()]
            try:
                try:
                    out = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                except subprocess.TimeoutExpired:  # open for an hour: treat as cancelled
                    on_done(None)
                    return
                if out.returncode == 0:
                    on_done(RangeAnswer.model_validate_json(out.stdout.strip()))
                    return
                if out.returncode == CANCELLED:
                    on_done(None)
                    return
                answer = fallback()
            except Exception:  # noqa: BLE001 - neither GTK nor zenity: do nothing
                answer = None
            on_done(answer)

        threading.Thread(target=ask, daemon=True, name="mnema-bar-range").start()

    def trash(self, path: Path) -> None:
        """To the Trash (gio, the file manager's Trash), not deleted for good."""
        out = subprocess.run(
            ["gio", "trash", str(path)], capture_output=True, text=True, timeout=60
        )
        if out.returncode != 0:
            raise OSError(out.stderr.strip() or _("The Trash is unavailable"))

    def open_form(self, ask: Any, on_event: Any) -> Any:
        """A live GTK form in the same GLib loop as the settings window."""
        from .linux_forms import open_form

        def changed(event: Any) -> None:
            try:
                on_event(event)
            finally:
                if self._on_form_change:
                    self._on_form_change()

        parent = self._settings_parent() if self._settings_parent else None
        return open_form(ask, changed, parent=parent)

    def open_settings(self, tab: str) -> None:
        if self._on_settings:
            self._on_settings(tab)

    def confirm(  # noqa: A002
        self, title: str, message: str, ok: str, on_done: Any, *, cancel: str | None = None
    ) -> None:
        """A confirmation through zenity; without zenity nothing is confirmed."""

        def ask() -> None:
            cmd = [
                "zenity", "--question", f"--title={title}", f"--text={message}",
                f"--ok-label={ok}", f"--cancel-label={cancel or _('Cancel')}", "--default-cancel",
            ]  # fmt: skip
            try:
                out = subprocess.run(cmd, capture_output=True, timeout=600)
            except Exception:  # noqa: BLE001
                on_done(False)
                return
            on_done(out.returncode == 0)

        threading.Thread(target=ask, daemon=True, name="mnema-bar-confirm").start()

    def notify(self, title: str, message: str, path: Path | None) -> None:  # noqa: A002
        def show() -> None:
            from .install import _linux_icon_path

            base = ["notify-send", "-a", "Mnema", "-i", str(_linux_icon_path()), title, message]
            try:
                if path is None:
                    subprocess.run(base, capture_output=True, timeout=10)
                    return
                # -A waits for a click and prints the action name (libnotify ≥ 0.7.9, Ubuntu 22.04+)
                action = "open=" + _("Open")
                out = subprocess.run([*base, "-A", action], capture_output=True, text=True)
                if out.returncode == 0 and out.stdout.strip() == "open":
                    self.open(followed(path))
                elif out.returncode != 0:
                    subprocess.run(base, capture_output=True, timeout=10)
            except Exception:  # noqa: BLE001 - notifications are best-effort
                pass

        threading.Thread(target=show, daemon=True, name="mnema-bar-notify").start()


def _signature(items: list[MenuItem]) -> tuple[Any, ...]:
    """A menu fingerprint: unchanged means nothing to rebuild."""
    return tuple(
        (it.title, it.key, it.checked, it.enabled, it.separator, _signature(it.children))
        for it in items
    )


class TrayApp:
    def __init__(self) -> None:
        self._icon: Any = None
        self._kind = ""
        self._shown: tuple[Any, ...] = ()  # what the panel shows now: label, title, menu
        self._lock = threading.Lock()
        self._settings: Any = None
        self._settings_scheduled = False
        self._settings_present = False
        self._settings_tab: str | None = None  # the tab to open the window on
        self._library: Any = None
        self._library_scheduled = False
        self._library_present = False
        self.runtime = BarRuntime(
            on_change=self.refresh,
            platform=LinuxPlatform(
                on_form_change=self.refresh,
                settings_parent=lambda: self._settings.window if self._settings else None,
                on_settings=lambda tab: self._schedule_settings(show=True, tab=tab),
            ),
        )

    def _schedule_library(self, *, show: bool = False) -> None:
        with self._lock:
            if not show and (self._library is None or not self._library.visible):
                return
            self._library_present |= show
            if self._library_scheduled:
                return
            self._library_scheduled = True
        from gi.repository import GLib  # ty: ignore[unresolved-import]

        GLib.idle_add(self._update_library)

    def _update_library(self) -> bool:
        from ..config import library_root
        from .linux_library import LibraryWindow
        from .panel import panel_model
        from .recordings import library, with_folders
        from .settings_model import settings_model

        with self._lock:
            self._library_scheduled = False
            show, self._library_present = self._library_present, False
        rt = self.runtime
        if rt.can_exit:
            if self._library:
                self._library.close()
                self._library = None
            return False
        if self._library is None:
            self._library = LibraryWindow(self._settings_action)
        view = library(library_root(), exclude=rt.active_folder, connection=rt.sync_connection())
        # queued folders show even outside the library
        cards = with_folders(view.cards, rt.job_folders())
        env = rt.menu_env()
        settings = settings_model(
            rt.prefs, env, hotkey_error=rt.hotkey_error, idle=rt.state.phase == Phase.IDLE
        )
        model = panel_model(rt.state, rt.prefs, env, cards, settings, sections=view.sections)
        if show:
            self._library.show(model)
        else:
            self._library.update(model)
        return False

    def _menu_items(self) -> list[MenuItem]:
        rt = self.runtime
        sections = rt.record_sections() if rt.state.phase == Phase.IDLE else []
        items = menu(rt.state, rt.prefs, rt.menu_env(), sections=sections)
        index = next((i for i, item in enumerate(items) if item.key == "open_folder"), len(items))
        items.insert(index, MenuItem(key="library", title=_("Recordings")))
        return items

    def _schedule_settings(self, *, show: bool = False, tab: str | None = None) -> None:
        """Background callbacks only schedule work; GTK stays on the main thread."""
        with self._lock:
            if self._settings is None and not show:
                return
            self._settings_present |= show
            self._settings_tab = tab or self._settings_tab
            if self._settings_scheduled:
                return
            self._settings_scheduled = True
        from gi.repository import GLib  # ty: ignore[unresolved-import]

        GLib.idle_add(self._update_settings)

    def _update_settings(self) -> bool:
        from .linux_settings import SettingsWindow
        from .settings_model import settings_model

        with self._lock:
            self._settings_scheduled = False
            show, self._settings_present = self._settings_present, False
            tab, self._settings_tab = self._settings_tab, None
        rt = self.runtime
        if rt.can_exit:
            if self._settings:
                self._settings.close()
                self._settings = None
            return False
        if self._settings is None:
            self._settings = SettingsWindow(self._settings_action)
        model = settings_model(
            rt.prefs, rt.menu_env(), hotkey_error=rt.hotkey_error, idle=rt.state.phase == Phase.IDLE
        )
        if show:
            self._settings.show(model, tab=tab)
        else:
            self._settings.update(model)
        return False

    def _settings_action(self, key: str) -> None:
        self.runtime.handle(key)
        self.refresh()

    # ── menu from the shared description ──────────────────────────────────────

    def _to_pystray(self, items: list[MenuItem]) -> list[Any]:
        pystray = importlib.import_module("pystray")  # Linux-only: ty on a Mac cannot see it

        out: list[Any] = []
        for it in items:
            if it.separator:
                out.append(pystray.Menu.SEPARATOR)
                continue
            sub = pystray.Menu(*self._to_pystray(it.children)) if it.children else None
            key = it.key

            # _k is keyword-only: pystray counts co_argcount and allows at most 2 positional
            # arguments; a third made MenuItem raise ValueError and left the icon PASSIVE
            def act(icon: Any, item: Any, *, _k: str | None = key) -> None:
                if _k == "settings":
                    self._schedule_settings(show=True)
                elif _k == "library":
                    self._schedule_library(show=True)
                elif _k:
                    self.runtime.handle(_k)

            out.append(
                pystray.MenuItem(
                    it.title,
                    sub if sub is not None else act,
                    checked=(lambda _i, _c=it.checked: _c)
                    if it.key and it.key not in {"start", "settings"}
                    else None,
                    radio=it.radio,
                    enabled=it.enabled and (bool(it.key) or sub is not None),
                )
            )
        return out

    def _menu(self) -> Any:
        pystray = importlib.import_module("pystray")  # Linux-only: ty on a Mac cannot see it

        return pystray.Menu(lambda: iter(self._to_pystray(self._menu_items())))

    # ── icon and menu updates (any thread: pystray hops to GLib itself) ────────

    def refresh(self) -> None:
        self._schedule_settings()
        self._schedule_library()
        icon = self._icon
        if icon is None:
            return
        from .icons import tray_icon

        with self._lock:
            rt = self.runtime
            t = title(rt.state, discreet=rt.prefs.discreet, coarse=True)
            if t.icon != self._kind:
                icon.icon = tray_icon(t.icon)
                self._kind = t.icon
            # touch the panel only on changes: AppIndicator rebuilds the whole menu, and
            # update_menu every half second of a recording flickered visibly
            items = _signature(self._menu_items())
            old_text, old_items = self._shown or (None, None)
            if t.text != old_text:  # the indicator title is left alone: changing it redraws
                self._set_label(icon, t.text)
            if items != old_items:
                icon.update_menu()
            self._shown = (t.text, items)
        rt = self.runtime
        if rt.can_exit:
            icon.stop()

    @staticmethod
    def _set_label(icon: Any, text: str) -> None:
        """The label next to the icon (the timer); only the AppIndicator backend has one."""
        ind = getattr(icon, "_appindicator", None)
        if ind is None:
            return
        try:
            from gi.repository import GLib  # ty: ignore[unresolved-import]

            width = _("000 min")  # the widest label, so the panel does not jump
            GLib.idle_add(lambda: ind.set_label(text, width) and False)
        except Exception:  # noqa: BLE001
            pass

    def _ticker(self) -> None:
        while True:
            time.sleep(_TICK_SECONDS)
            self.runtime.tick()
            self._schedule_library()
            if self.runtime.state.busy:
                self.refresh()  # touches the panel only when the menu or label changed

    def run(self) -> None:
        pystray = importlib.import_module("pystray")  # Linux-only: ty on a Mac cannot see it

        from .icons import tray_icon

        self._icon = pystray.Icon("mnema", icon=tray_icon("idle"), title="Mnema", menu=self._menu())
        self._kind = "idle"
        threading.Thread(target=self._ticker, daemon=True, name="mnema-bar-tick").start()
        self.runtime.start_sync()
        try:
            self._icon.run()
        finally:
            self.runtime.close()


def run() -> None:
    try:
        importlib.import_module("pystray")  # Linux-only: ty on a Mac cannot see it
    except Exception as e:
        raise SystemExit(_("Mnema isn't fully installed. Reinstall it: ./install.sh client")) from e
    TrayApp().run()
