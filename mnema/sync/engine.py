"""Three-way sync: client files, the full server list, the previous checkpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from ..i18n import _
from ..library.disk import LibraryError, fsync_dir, read_file, read_record, safe_path
from ..library.models import FileBody, FileName, Manifest, Record, Stamp
from ..live.naming import display_title, free_name, same_folder
from . import state
from .client import LibraryClient
from .scan import FILES_LOCK, Scan, SyncError, created_ns, scan, stamp, write_file

logger = logging.getLogger(__name__)
# Shown through _() where it is reported.
SYNC_FAILED = "Could not sync recordings with the server. See the Mnema log for details"


class SyncStatus(BaseModel):
    connection: str | None = Field(default=None, exclude=True, repr=False)
    bulk_missing: list[str] = Field(default_factory=list)
    bulk_on_server: bool = False  # bulk_missing vanished on the server, not on this device
    errors: list[str] = Field(default_factory=list)
    changed: bool = False


def _hash(value: Stamp | None) -> str | None:
    return value.sha256 if value else None


def match_case(root: Path, relative: str, busy: set[str]) -> str:
    """The folder is already at relative, but case-insensitive storage (APFS, the phone) kept the
    section or recording in its old case: "work/Daily" instead of "Work/Daily". Names on disk
    are changed to relative, a section only when none of its recordings is busy; otherwise the next
    walk would take the old case for a rename made here. Returns the path as it is on disk."""
    parent, parts, actual = root, relative.split("/"), []
    for index, part in enumerate(parts):
        wanted, names, name = parent / part, os.listdir(parent), part
        found = (
            None
            if part in names
            else next((n for n in names if same_folder(parent / n, wanted)), None)
        )
        if found is not None:
            name = found
            inside = "/".join([*actual, found])
            absolute = str(root / inside)
            held = index < len(parts) - 1 and any(
                b in (inside, absolute) or b.startswith((inside + "/", absolute + "/"))
                for b in busy
            )
            if not held:
                with suppress(OSError):  # keep the old name: the path below follows the disk
                    (parent / found).rename(wanted)
                    fsync_dir(parent)
                if part in os.listdir(parent):
                    name = part
        actual.append(name)
        parent = parent / name
    return "/".join(actual)


def shown_title(path: str) -> str:
    """A recording or section as the feed shows it: the folder name without the date."""
    name = path.rpartition("/")[2]
    return display_title(name).title or name


def record_error(record: Record, reason: str) -> str:
    """A sync error about one recording starts with its title."""
    return _("“{title}”: {error}", title=shown_title(record.path), error=reason)


def nested_error(path: str) -> str:
    """path: "Recording/Inner" or "Section/Recording/Inner"."""
    outer = path.rpartition("/")[0]
    return _(
        "“{title}” is inside the recording “{outer}”. Move it into a section: until then sync "
        "skips both and deletes nothing on the server",
        title=shown_title(path),
        outer=shown_title(outer),
    )


def deep_error(path: str) -> str:
    """path: "Section/Subfolder/Recording"."""
    return _(
        "“{title}” is in a subfolder of the section “{section}”. Move it into the section "
        "itself: until then sync skips it and deletes nothing on the server",
        title=shown_title(path),
        section=path.partition("/")[0],
    )


class SyncEngine:
    def __init__(
        self,
        root: Path,
        state_path: Path,
        *,
        selected: Callable[[], str | None],
        busy: Callable[[], set[str]],
        trash: Callable[[Path], None],
        client_factory: Callable[[str], LibraryClient] = LibraryClient,
    ) -> None:
        self.root, self.state_path = root.absolute(), state_path
        self.real = self.root.resolve()  # a root link is the same library; scan catches a swap
        self.selected, self.busy, self.trash = selected, busy, trash
        self.client_factory = client_factory
        self._lock = asyncio.Lock()

    async def run_once(self) -> SyncStatus:
        async with self._lock:
            return await self._run(None)

    async def resolve_bulk(
        self, ids: list[str], restore: bool, *, connection: str | None = None
    ) -> SyncStatus:
        async with self._lock:
            return await self._run((set(ids), restore), expected=connection)

    async def _run(
        self, choice: tuple[set[str], bool] | None, *, expected: str | None = None
    ) -> SyncStatus:
        connection = self.selected()
        status = SyncStatus(connection=connection)
        if expected is not None and connection != expected:
            status.errors.append(_("The sync server changed; choose the action after the sync"))
            return status
        if not connection:
            return status
        client = None
        try:
            client = self.client_factory(connection)
            await _Cycle(self, client, connection, status).run(choice)
        except LibraryError as error:
            status.errors.append(str(error))
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("sync with the server failed", exc_info=True)
            status.errors.append(_(SYNC_FAILED))
        finally:
            if client is not None:
                await client.aclose()
        return status


class _Cycle:
    def __init__(
        self, engine: SyncEngine, client: LibraryClient, connection: str, status: SyncStatus
    ) -> None:
        self.engine, self.client, self.connection, self.status = engine, client, connection, status
        self.root, self.real = engine.root, engine.real
        previous = state.load(engine.state_path)
        self.known_root = previous.server == client.server
        self.saved = previous
        # Sections of the last sync describe local folders, so they apply after a server change too.
        self.known_sections = frozenset(previous.sections)
        self.previous = (
            previous.model_copy(deep=True)
            if self.known_root
            else state.Checkpoint(server=client.server)
        )
        self.local = Scan()

    def selected(self) -> None:
        if self.engine.selected() != self.connection:
            raise SyncError(_("The sync server changed; a new sync will start"))

    async def call[T](self, action: Awaitable[T]) -> T:
        result = await action
        self.selected()  # a response from the previous server must not change the new one's files
        return result

    def save(self) -> None:
        self.selected()
        if self.previous == self.saved:
            return  # a sync with no changes does not rewrite sync.json
        state.save(self.engine.state_path, self.previous)
        self.saved = self.previous.model_copy(deep=True)

    def blocked(self, record: Record, *, view: Scan | None = None) -> bool:
        busy = self.engine.busy()
        return (
            record.id in busy
            or record.path in busy
            or str(self.root / record.path) in busy
            or any(
                p == "." or record.path == p or record.path.startswith(p + "/")
                for p in (view if view is not None else self.local).blocked_paths
            )
        )

    def current(self, record: Record) -> Record:
        self.selected()
        if self.blocked(record):
            raise SyncError(_("The recording is busy; sync will wait"))
        current = read_record(self.root, record.path, self.real)
        if current.id != record.id:
            raise SyncError(_("The recording folder has already changed"))
        return current

    def meta(self, record: Record, *, new_id: bool = False) -> Record:
        with FILES_LOCK:
            current = self.current(record)
            path = safe_path(self.root, current.path, self.real) / FileName.META
            raw, expected = read_file(path)
            data = json.loads(raw)
            if new_id:
                data["id"] = str(uuid4())
            write_file(
                path,
                (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(),
                time.time_ns(),
                expected,
            )
            self.status.changed = True
            return read_record(self.root, current.path, self.real)

    def remote(self, manifest: Manifest) -> dict[str, Record]:
        for name in manifest.sections:
            if "/" in name:
                raise SyncError(_("The server returned a nested section"))
            safe_path(self.root, name, self.real)
        records: dict[str, Record] = {}
        paths: set[str] = set()
        for record in manifest.records:
            safe_path(self.root, record.path, self.real)
            if record.id in records or record.path in paths or FileName.META not in record.files:
                raise SyncError(_("The server returned an incomplete list of recordings"))
            records[record.id] = record
            paths.add(record.path)
        return records

    def remember(self, record: Record) -> None:
        if self.previous.records.get(record.id) != record:
            self.previous.records[record.id] = record.model_copy(deep=True)
            self.save()

    def forget(self, id: str) -> None:
        if self.previous.records.pop(id, None) is not None:
            self.save()

    def scan(self) -> Scan:
        return scan(
            self.root,
            self.engine.busy(),
            sections=self.known_sections,
            records={r.path for r in self.previous.records.values()},
            real=self.real,
        )

    async def run(self, choice: tuple[set[str], bool] | None) -> None:
        remote = self.remote(await self.call(self.client.manifest()))
        self.local = self.scan()
        if self.local.missing_root:
            if not self.known_root:
                raise SyncError(
                    _("The Mnema folder was not found; the first sync does not create it")
                )
            self.local.complete = True
        if not self.local.complete:
            if not self.local.readable or not (self.local.nested or self.local.deep):
                self.status.errors.append(
                    _("Some local recordings could not be read; deletions are postponed")
                )
            self.status.errors.extend(map(nested_error, self.local.nested))
            self.status.errors.extend(map(deep_error, self.local.deep))
        groups: dict[str, list[Record]] = defaultdict(list)
        for record in self.local.manifest.records:
            groups[record.id].append(record)
        local: dict[str, Record] = {}
        for id, group in groups.items():
            old = self.previous.records.get(id)
            if len(group) > 1:
                # The copy is the one off the previous path, or else the folder created later.
                group.sort(
                    key=lambda r: (
                        r.path != (old.path if old else ""),
                        created_ns(safe_path(self.root, r.path, self.real)),
                        r.path,
                    )
                )
            for index, record in enumerate(group):
                if index:
                    record = self.meta(record, new_id=True)
                local[record.id] = record
        missing = {
            id
            for id, record in self.previous.records.items()
            if self.local.complete and id not in local and not self.blocked(record)
        }
        vanished = {
            id
            for id, record in self.previous.records.items()
            if id in local and id not in remote and not self.blocked(record)
        }
        # Two or more missing on one side wait for a choice; this device's go first.
        asks = [(g, server) for g, server in ((missing, False), (vanished, True)) if len(g) >= 2]
        if choice is not None:
            ids, restore = choice
            if not asks or ids != asks[0][0]:
                raise SyncError(_("The list of missing recordings changed; sync again"))
            if restore:
                for id in ids:
                    self.previous.records.pop(id, None)
                self.save()  # the whole set before the first transfer, also after a restart
            asks.pop(0)
        held = {id for group, _server in asks for id in group}
        if asks:
            self.status.bulk_missing, self.status.bulk_on_server = sorted(asks[0][0]), asks[0][1]
        if held and self.local.missing_root:
            return
        ids = set(local) | set(remote) | set(self.previous.records)
        for id in sorted(ids):
            left, right, old = local.get(id), remote.get(id), self.previous.records.get(id)
            if id in held or any(self.blocked(r) for r in (left, right, old) if r is not None):
                continue
            try:
                if left is None and old is not None:
                    if not self.local.complete:
                        continue
                    fresh = self.scan()
                    if (
                        (not fresh.complete and not fresh.missing_root)
                        or any(r.id == id for r in fresh.manifest.records)
                        or any(self.blocked(r, view=fresh) for r in (old, right) if r is not None)
                    ):
                        raise SyncError(_("Local files changed; deletion is postponed"))
                    if right:
                        await self.call(self.client.remove(id, right.tag))
                    self.forget(id)
                    self.status.changed = True
                elif right is None and old is not None and left is not None:
                    with FILES_LOCK:
                        current = self.current(left)
                        if current.files != left.files:
                            raise SyncError(
                                _("The local recording has already changed; deletion is postponed")
                            )
                        self.engine.trash(safe_path(self.root, current.path, self.real))
                        self.forget(id)
                        self.status.changed = True
                elif left is not None and right is None:
                    if {FileName.TRANSCRIPT, FileName.SUMMARY} & left.files.keys():
                        await self.publish(left)
                elif right is not None and left is None:
                    await self.download(right)
                elif left is not None and right is not None:
                    await self.merge(left, right, old)
            except (OSError, ValueError, KeyError, TypeError, LibraryError) as error:
                self.selected()  # a server change ends the cycle with its own message
                reason = (
                    str(error)
                    if isinstance(error, LibraryError)
                    else _("Could not sync the recording")
                )
                self.status.errors.append(
                    record_error(next(r for r in (left, old, right) if r), reason)
                )
        if not held and self.local.complete:
            await self.sections()
        if self.local.complete:
            self.save()

    def destination(self, relative: str, source: str | None = None) -> Path:
        dest = safe_path(self.root, relative, self.real)
        if dest.parent != self.root and (dest.parent / FileName.META).exists():
            raise SyncError(_("A recording already has the section name"))
        candidate = free_name(dest, own=self.root / source if source else None)
        return safe_path(self.root, candidate.relative_to(self.root).as_posix(), self.real)

    async def common_path(self, right: Record, source: str | None = None) -> Record:
        while True:
            dest = self.destination(right.path, source)
            relative = dest.relative_to(self.root).as_posix()
            if relative == right.path:
                return right
            right = await self.call(self.client.move(right.id, relative, right.path))
            self.status.changed = True

    async def publish(self, left: Record) -> None:
        # Neither on the server nor in the checkpoint: publish under its own id, copies already
        # have their own ids; a server change makes no duplicates, a restore from the Trash is a
        # new recording again.
        left = self.current(left)
        files: dict[FileName, FileBody] = {}
        for name, expected in left.files.items():
            raw, current = read_file(safe_path(self.root, left.path, self.real) / name)
            if current != expected:
                raise SyncError(_("The file changed before upload"))
            files[name] = FileBody(text=raw.decode(), mtime_ns=current.mtime_ns)
        self.current(left)
        right = await self.call(self.client.create(left.id, left.path, files))
        old = right.model_copy(update={"path": left.path}, deep=True)
        self.remember(old)  # the accepted bytes; a newer local edit stays a change from them
        self.status.changed = True
        await self.merge(self.current(left), right, old)

    async def download(self, right: Record) -> None:
        right = await self.common_path(right)
        raw, received = await self.call(self.client.read(right.id, FileName.META))
        if received != right.files[FileName.META] or json.loads(raw).get("id") != right.id:
            raise SyncError(_("The recording on the server changed during download"))
        with FILES_LOCK:
            self.selected()
            if self.blocked(right):
                raise SyncError(_("The recording is busy; sync will wait"))
            dest = safe_path(self.root, right.path, self.real)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.mkdir()  # a collision or race never replaces an existing folder
            fsync_dir(self.root.parent)
            fsync_dir(self.root)
            fsync_dir(dest.parent)
            try:
                write_file(dest / FileName.META, raw, received.mtime_ns, None)
            finally:
                if not list(dest.iterdir()):
                    dest.rmdir()
            left = read_record(
                self.root, match_case(self.root, right.path, self.engine.busy()), self.real
            )
            old = left.model_copy(update={"tag": right.tag}, deep=True)
            self.remember(old)
            self.status.changed = True
        await self.merge(left, right, old)

    async def merge(self, left: Record, right: Record, old: Record | None) -> None:
        if left.path != right.path:
            # Without a previous sync (a new server, a lost sync.json) the server's path wins.
            if old is not None and left.path != old.path and right.path != old.path:
                raise SyncError(
                    _("Renamed differently here and on the server; files are left in place")
                )
            if old is not None and left.path != old.path:
                self.current(left)
                right = await self.call(self.client.move(left.id, left.path, right.path))
            right = await self.common_path(right, left.path)
            with FILES_LOCK:
                current = self.current(left)
                source, dest = (
                    safe_path(self.root, current.path, self.real),
                    safe_path(self.root, right.path, self.real),
                )
                if source != dest:
                    same = same_folder(dest, source)  # the same name in another case: APFS
                    if (dest.exists() or dest.is_symlink()) and not same:
                        raise SyncError(_("The new name is already taken"))
                    if not same:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        source.rename(dest)
                        fsync_dir(source.parent)
                        fsync_dir(dest.parent)
                    self.status.changed = True
                left = read_record(
                    self.root, match_case(self.root, right.path, self.engine.busy()), self.real
                )
        base = (
            old.model_copy(deep=True)
            if old
            else Record(id=left.id, path=left.path, files={}, tag=right.tag)
        )
        base.path, base.tag = left.path, right.tag
        self.remember(base)
        for name in FileName:
            local_stamp, remote_stamp, previous = (
                left.files.get(name),
                right.files.get(name),
                base.files.get(name),
            )
            if _hash(local_stamp) == _hash(remote_stamp):
                chosen = remote_stamp
            else:
                local_changed = _hash(local_stamp) != _hash(previous)
                remote_changed = _hash(remote_stamp) != _hash(previous)
                if local_changed and remote_changed:
                    if (
                        local_stamp is None
                        or remote_stamp is None
                        or local_stamp.mtime_ns == remote_stamp.mtime_ns
                    ):
                        raise SyncError(
                            _("Both sides edited the file; which edit is newer is unknown")
                        )
                    upload = local_stamp.mtime_ns > remote_stamp.mtime_ns
                else:
                    upload = local_changed
                path = safe_path(self.root, left.path, self.real) / name
                if upload:
                    self.current(left)
                    if stamp(path) != local_stamp:
                        raise SyncError(_("The local file has already changed"))
                    if local_stamp:
                        raw, current = read_file(path)
                        if current != local_stamp:
                            raise SyncError(_("The local file has already changed"))
                        chosen = await self.call(
                            self.client.write(
                                left.id, name, raw, local_stamp.mtime_ns, _hash(remote_stamp)
                            )
                        )
                    else:
                        assert remote_stamp is not None
                        await self.call(self.client.remove_file(left.id, name, remote_stamp.sha256))
                        chosen = None
                else:
                    raw = b""
                    if remote_stamp:
                        raw, received = await self.call(self.client.read(left.id, name))
                        if received != remote_stamp:
                            raise SyncError(_("The server file has already changed"))
                    with FILES_LOCK:
                        self.current(left)
                        if remote_stamp:
                            write_file(path, raw, remote_stamp.mtime_ns, local_stamp)
                        else:
                            if stamp(path) != local_stamp:
                                raise SyncError(_("The local file has already changed"))
                            path.unlink()
                            fsync_dir(path.parent)
                    chosen = remote_stamp
                self.status.changed = True
            if chosen:
                base.files[name] = chosen
            else:
                base.files.pop(name, None)
            self.remember(base)

    async def sections(self) -> None:
        local = self.scan()
        if not local.complete and not local.missing_root:
            return
        remote = await self.call(self.client.manifest())
        self.remote(remote)  # check the whole response before changing any section
        left, right, old = (
            set(local.manifest.sections),
            set(remote.sections),
            set(self.previous.sections),
        )
        for name in sorted(left | right | old):
            if "/" in name:
                raise SyncError(_("The server returned a nested section"))
            path = safe_path(self.root, name, self.real)
            if any(p == "." or p == name or p.startswith(name + "/") for p in local.blocked_paths):
                continue
            self.selected()
            if name in left and name not in right:
                if name in old:
                    if list(path.iterdir()):
                        continue
                    path.rmdir()
                    fsync_dir(path.parent)
                    self.status.changed = True
                else:
                    await self.call(self.client.section(name))
                    right.add(name)
                    self.status.changed = True
            elif name in right and name not in left:
                if name in old:
                    await self.call(self.client.section(name, delete=True))
                    right.remove(name)
                    self.status.changed = True
                else:
                    # the same section in another case (APFS): rename it rather than create a second
                    variant = next(
                        (n for n in left if n != name and same_folder(self.root / n, path)), None
                    )
                    if variant is not None and any(
                        p == variant or p.startswith(variant + "/") for p in local.blocked_paths
                    ):
                        continue
                    if variant is not None:
                        (self.root / variant).rename(path)
                        left.discard(variant)
                    else:
                        path.mkdir(parents=True, exist_ok=True)
                    fsync_dir(path.parent)
                    left.add(name)
                    self.status.changed = True
            if name in left and name in right:
                old.add(name)
            elif name not in left or name not in right:
                old.discard(name)
            self.previous.sections = sorted(old)
            self.save()
