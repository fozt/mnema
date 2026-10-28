package dev.mnema.app.sync

import dev.mnema.app.i18n.tr
import dev.mnema.app.recordings.MediaIndex
import dev.mnema.app.recordings.changeCase
import dev.mnema.app.recordings.freeName
import dev.mnema.app.recordings.sameFolder
import dev.mnema.app.server.Connection
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** Three-way sync: the disk, the server's full manifest, and the last checkpoint. */
class LibrarySync(
    private val root: File,
    private val stateFile: File,
    private val selected: () -> Connection?,
    private val busy: () -> Set<String>,
    private val trash: (File) -> Unit,
    private val clientFactory: (Connection) -> LibraryClient = { LibraryClient(it) },
    // without a trash, what was deleted on the server is deleted here only after "Delete forever"
    private val hasTrash: () -> Boolean = { true },
    // moves a folder to the server's name (and each of the two case-change steps); if Android refuses (EPERM from
    // MediaProvider), a rename keeps the old name everywhere and a case change waits for the next sync
    private val move: (File, File) -> Unit = { from, to -> MediaIndex.retrying(from) { Files.move(from.toPath(), to.toPath()) } },
    // sync decisions about section case go to the log (logcat MnemaSync); tests stay silent
    private val log: (String) -> Unit = {},
) {
    private val lock = Mutex()
    suspend fun runOnce(): SyncStatus = lock.withLock { withContext(Dispatchers.IO) { run(null) } }
    suspend fun resolveBulk(ids: List<String>, restore: Boolean, forever: Boolean = false): SyncStatus = lock.withLock {
        withContext(Dispatchers.IO) { run(Choice(ids.toSet(), restore, forever)) }
    }

    private class Choice(val ids: Set<String>, val restore: Boolean, val forever: Boolean)

    private suspend fun run(choice: Choice?): SyncStatus {
        val status = SyncStatus()
        val connection = selected() ?: return status
        try {
            clientFactory(connection).use { Cycle(it, connection, status).run(choice) }
        } catch (e: IOException) { status.errors += message(e) }
        catch (_: IllegalArgumentException) { status.errors += tr("Could not sync recordings with the server") }
        catch (_: NoSuchElementException) { status.errors += tr("Could not sync recordings with the server") }
        return status
    }

    private fun message(error: IOException) = if (error is SyncException) error.message.orEmpty() else tr("Could not sync recordings with the server")

    private inner class Cycle(val client: LibraryClient, val connection: Connection, val status: SyncStatus) {
        val root = this@LibrarySync.root.absoluteFile.normalize()
        val loaded = loadCheckpoint(stateFile)
        val knownRoot = loaded.server == client.server
        var previous = if (knownRoot) loaded else Checkpoint(server = client.server)
        val oldSections = previous.sections.toSet() // sections of the last sync as spelled, to tell who changed the case
        var saved = loaded
        val knownSections = loaded.sections.toSet() // about local folders, so it holds after a server change too
        var local = Scan()
        var active = emptySet<String>() // busy snapshot for this pass; busy() is read again before each change
        var serverSections = emptySet<String>()

        fun selected() { if (this@LibrarySync.selected() != connection) throw SyncException(tr("The sync server changed; a new sync will start")) }
        suspend fun <T> call(action: suspend () -> T): T { selected(); val result = action(); selected(); return result }
        fun save() {
            selected()
            if (previous == saved) return // an idle sync does not rewrite the checkpoint
            saveCheckpoint(stateFile, previous)
            saved = previous
        }
        fun remember(record: Record) { if (previous.records[record.id] != record) { previous = previous.copy(records = previous.records + (record.id to record)); save() } }
        fun forget(id: String) { if (id in previous.records) { previous = previous.copy(records = previous.records - id); save() } }
        // a server or last-sync path may differ in case from the disk (samePath) and still be the same folder
        fun covered(paths: List<String>, path: String) = paths.any { it == "." || within(path, it) }
        fun blocked(record: Record, busy: Set<String> = active): Boolean {
            val absolute = File(root, record.path).absolutePath
            return record.id in busy || busy.any { samePath(it, record.path) || samePath(it, absolute) } || covered(local.blockedPaths, record.path)
        }

        fun scanLocal(busy: Set<String>) = scan(root, busy, sections = knownSections, records = previous.records.values.mapTo(mutableSetOf()) { it.path })

        fun current(record: Record): Record {
            selected()
            if (blocked(record, busy())) throw SyncException(tr("The recording is busy; sync will wait"))
            return readRecord(root, record.path).also { if (it.id != record.id) throw SyncException(tr("The recording folder has already changed")) }
        }

        fun meta(record: Record, newId: Boolean = false): Record = synchronized(filesLock) {
            val current = current(record)
            val file = recordFile(root, current.path, FileName.META)
            val source = readFile(file)
            val data = wire.parseToJsonElement(utf8(source.bytes)).jsonObject.toMutableMap()
            if (newId) data["id"] = JsonPrimitive(UUID.randomUUID().toString())
            writeFile(file, (JsonObject(data).toString() + "\n").toByteArray(), nowNs(), source.stamp)
            status.changed = true
            readRecord(root, current.path)
        }

        fun remote(manifest: Manifest): Map<String, Record> {
            val records = manifest.records.associateBy { it.id }
            if (records.size != manifest.records.size || records.values.map { it.path }.toSet().size != records.size || records.values.any { FileName.META !in it.files }) {
                throw SyncException(tr("The server returned an incomplete list of recordings"))
            }
            records.values.forEach { safePath(root, it.path) }
            manifest.sections.forEach { if ('/' in it) throw SyncException(tr("The server returned a nested section")) else safePath(root, it) }
            return records
        }

        suspend fun run(choice: Choice?) {
            val manifest = call { client.manifest() }
            val remote = remote(manifest)
            serverSections = manifest.sections.toSet()
            active = busy()
            local = scanLocal(active)
            if (local.missingRoot) {
                if (!knownRoot) throw SyncException(tr("The Mnema folder was not found; the first sync does not create it"))
                local = local.copy(complete = true)
            } else if (followServerCase(remote.values)) local = scanLocal(active)
            if (!local.complete) {
                status.errors += tr("Some local recordings could not be read; deletions are postponed")
                local.nested.forEach { status.errors += tr("The recording “{path}” is inside another recording", "path" to it) }
                local.deep.forEach { status.errors += tr("The recording “{path}” is in a section subfolder", "path" to it) }
                local.withoutFiles.forEach { status.errors += tr("The recording folder “{path}” has none of its files", "path" to it) }
            }
            val lefts = mutableMapOf<String, Record>()
            for ((id, group) in local.manifest.records.groupBy { it.id }) {
                // the copy is the one off the previous path, or without one the folder created later
                val ordered = if (group.size < 2) group else keeperFirst(group, previous.records[id]?.path, { it.path }) { safePath(root, it.path) }
                ordered.forEachIndexed { index, record ->
                    val unique = if (index == 0) record else meta(record, newId = true)
                    lefts[unique.id] = unique
                }
            }
            val missing = previous.records.filter { (id, record) -> local.complete && id !in lefts && !blocked(record) }.keys
            // the server lost several recordings at once: local copies are not deleted without a choice
            val gone = previous.records.filter { (id, record) -> id in lefts && id !in remote && !blocked(record) && !blocked(lefts.getValue(id)) }.keys
            val noTrash = !hasTrash()
            status.withoutTrash = noTrash
            var held = if (missing.size >= 2) missing else emptySet()
            // without a trash even one recording gone on the server waits for a choice: nothing is silently deleted forever
            var heldOnServer = if (gone.size >= 2 || noTrash && gone.isNotEmpty()) gone else emptySet()
            var forever = emptySet<String>()
            if (choice != null) {
                val ids = choice.ids
                val here = held.isNotEmpty() // losses on both sides are asked about in turn, this device first
                if (ids.isEmpty() || ids != (if (here) held else heldOnServer)) throw SyncException(tr("The list of missing recordings changed; sync again"))
                if (choice.restore) {
                    previous = previous.copy(records = previous.records - ids)
                    save() // the whole set is excluded before the first download or upload, so a restart will not delete the rest
                }
                if (here) held = emptySet()
                // the trash was chosen but is gone now: ask again, this time about deleting forever
                else if (choice.restore || choice.forever || !noTrash) heldOnServer = emptySet()
                if (!here && choice.forever) forever = ids
            }
            status.bulkMissing += held.sorted()
            if (held.isEmpty()) status.serverMissing += heldOnServer.sorted()
            if (held.isNotEmpty() && local.missingRoot) return
            for (id in (lefts.keys + remote.keys + previous.records.keys).sorted()) {
                val left = lefts[id]; val right = remote[id]; val old = previous.records[id]
                if (id in held || id in heldOnServer || listOfNotNull(left, right, old).any(::blocked)) continue
                try {
                    when {
                        left == null && old != null -> {
                            if (!local.complete) continue
                            val now = busy()
                            val fresh = scanLocal(now)
                            if (blocked(old, now) || covered(fresh.blockedPaths, old.path) || (!fresh.complete && !fresh.missingRoot) || fresh.manifest.records.any { it.id == id }) throw SyncException(tr("Local files changed; deletion is postponed"))
                            if (right != null) call { client.remove(id, right.tag) }
                            forget(id); status.changed = true
                        }
                        right == null && old != null && left != null -> synchronized(filesLock) {
                            val current = current(left)
                            if (current.files != left.files) throw SyncException(tr("The local recording has already changed; deletion is postponed"))
                            discard(safePath(root, current.path), id in forever, hasTrash(), trash)
                            forget(id); status.changed = true
                        }
                        left != null && right == null -> if (FileName.TRANSCRIPT in left.files || FileName.SUMMARY in left.files) publish(left)
                        right != null && left == null -> download(right)
                        left != null && right != null -> merge(left, right, old)
                    }
                } catch (e: IOException) { status.errors += message(e); selected() }
                catch (_: IllegalArgumentException) { status.errors += tr("Could not sync the recording"); selected() }
                catch (_: NoSuchElementException) { status.errors += tr("Could not sync the recording"); selected() }
            }
            if (held.isEmpty() && heldOnServer.isEmpty() && local.complete) sections()
            if (local.complete) save()
        }

        fun destination(relative: String, source: String?): File {
            val dest = safePath(root, relative)
            if (dest.parentFile != root && File(dest.parentFile, FileName.META.fileName).exists()) throw SyncException(tr("A recording already has the section name"))
            val candidate = freeName(dest, own = source?.let { File(root, it) })
            return safePath(root, candidate.relativeTo(root).invariantSeparatorsPath)
        }

        suspend fun commonPath(record: Record, source: String? = null): Record {
            var right = record
            while (true) {
                val relative = destination(right.path, source).relativeTo(root).invariantSeparatorsPath
                if (relative == right.path) return right
                right = call { client.move(right.id, relative, right.path) }
                status.changed = true
            }
        }

        /** Section [here] was renamed here only by case: the last sync knew it in the server's spelling [there]. */
        fun renamedHere(here: String, there: String) = here !in oldSections && there in oldSections

        /**
         * A server section in another case is the same section (samePath): a new recording goes into it, not into a second
         * one. A section renamed by case here goes to the server in this device's spelling.
         */
        fun serverCase(path: String): String {
            val section = path.substringBefore('/', missingDelimiterValue = "")
            if (section.isEmpty() || section in serverSections) return path
            val there = serverSections.firstOrNull { samePath(it, section) } ?: return path
            return if (renamedHere(section, there)) path else "$there/${path.substringAfter('/')}"
        }

        /** The section holds a busy or unread recording, so its folder cannot be renamed now. */
        fun sectionHeld(section: String, now: Set<String>): Boolean {
            val absolute = File(root, section).absolutePath
            return local.blockedPaths.any { it == "." || within(it, section) } ||
                now.any { within(it, section) || within(it, absolute) } ||
                local.manifest.records.any { it.id in now && within(it.path, section) }
        }

        /**
         * A section renamed on the server only by case ("projects" → "Projects") gets the same name here through two real renames
         * via a temporary name (changeCase), never directly. A busy recording inside or an Android refusal keeps the old
         * name until the next sync. true — folders changed and the scan must run again.
         */
        fun followServerCase(records: Collection<Record>): Boolean {
            val spellings = serverSections + records.mapNotNull { it.path.substringBefore('/', "").ifEmpty { null } }
            var touched = false
            for (name in local.manifest.sections) {
                if (name in spellings) continue
                val wanted = spellings.filter { samePath(it, name) }.singleOrNull() ?: continue
                if (renamedHere(name, wanted)) { log("Section \"$name\": case changed here, sending \"$name\" to the server"); continue }
                synchronized(filesLock) {
                    selected()
                    if (sectionHeld(name, busy())) { log("Section \"$name\": a busy recording inside, \"$wanted\" later"); return@synchronized }
                    val from = safePath(root, name); val to = safePath(root, wanted)
                    if (exists(to) && !sameFolder(to, from)) { log("Section \"$name\": \"$wanted\" is another folder"); return@synchronized }
                    log("Section \"$name\" → \"$wanted\" as on the server")
                    touched = true
                    try {
                        changeCase(from, to, move)
                        syncDirectory(root)
                        status.changed = true
                    } catch (e: IOException) { log("Section \"$name\" not renamed: ${e.message}") /* old name kept; the next sync tries again */ }
                }
            }
            return touched
        }

        /** Under its own id: copies are already separated, so a server change does not create duplicates. */
        suspend fun publish(record: Record) {
            val left = current(record)
            val files = left.files.mapValues { (name, expected) ->
                val source = readFile(recordFile(root, left.path, name))
                if (source.stamp != expected) throw SyncException(tr("The file changed before upload"))
                FileBody(utf8(source.bytes), source.stamp.mtimeNs)
            }
            current(left)
            val right = call { client.create(left.id, serverCase(left.path), files) }
            val old = right.copy(path = left.path)
            remember(old) // the accepted bytes; a new edit after the POST stays a change relative to them
            status.changed = true
            merge(current(left), right, old)
        }

        suspend fun download(record: Record) {
            val right = commonPath(record)
            val received = call { client.read(right.id, FileName.META) }
            if (received.stamp != right.files[FileName.META] || wire.parseToJsonElement(utf8(received.bytes)).jsonObject["id"]?.jsonPrimitive?.content != right.id) throw SyncException(tr("The recording on the server changed during download"))
            val left = synchronized(filesLock) {
                selected()
                if (blocked(right, busy())) throw SyncException(tr("The recording is busy; sync will wait"))
                val dest = safePath(root, diskPath(root, right.path))
                Files.createDirectories(dest.parentFile!!.toPath())
                Files.createDirectory(dest.toPath())
                syncDirectory(root.parentFile!!); syncDirectory(root); syncDirectory(dest.parentFile!!)
                try { writeFile(File(dest, FileName.META.fileName), received.bytes, received.stamp.mtimeNs, null) }
                finally { if (dest.list()?.isEmpty() == true) Files.delete(dest.toPath()) }
                val local = readRecord(root, diskPath(root, right.path))
                remember(local.copy(tag = right.tag))
                status.changed = true
                local
            }
            merge(left, right, left.copy(tag = right.tag))
        }

        suspend fun merge(initialLeft: Record, initialRight: Record, old: Record?) {
            var left = initialLeft; var right = initialRight
            if (left.path != right.path && !samePath(left.path, right.path)) {
                if (old == null || (!samePath(left.path, old.path) && !samePath(right.path, old.path))) throw SyncException(tr("The folder was renamed differently on each side; files are left in place"))
                if (!samePath(left.path, old.path)) {
                    current(left)
                    right = call { client.move(left.id, serverCase(left.path), right.path) }
                }
                right = commonPath(right, left.path)
                val refused = synchronized(filesLock) {
                    val source = safePath(root, current(left).path)
                    // the section as it is on disk here: the server's "Projects/…" goes into "projects"
                    val dest = safePath(root, diskPath(root, right.path))
                    if (source != dest && !sameFolder(dest, source)) {
                        if (exists(dest)) throw SyncException(tr("The new name is already taken"))
                        Files.createDirectories(dest.parentFile!!.toPath())
                        try {
                            move(source, dest)
                        } catch (e: IOException) {
                            // the folder is still there: Android refused (EPERM from MediaProvider), a retry will not help
                            if (!source.isDirectory || exists(dest)) throw e
                            return@synchronized true
                        }
                        syncDirectory(source.parentFile!!); syncDirectory(dest.parentFile!!)
                        status.changed = true
                    }
                    left = readRecord(root, diskPath(root, right.path))
                    false
                }
                if (refused) {
                    // the recording name goes back to the server, otherwise every sync hits the same refusal
                    right = call { client.move(left.id, serverCase(left.path), right.path) }
                    status.errors += tr("Android did not allow renaming the folder “{path}”; the recording keeps its old name", "path" to left.path)
                    status.changed = true
                }
            } else if (left.path != right.path) {
                // the same folder differing only in case; the last sync tells who changed it: here — to the server (PATCH),
                // on the server — the recording name here via two renames (changeCase), the section via followServerCase
                val ours = left.path.split('/'); val theirs = right.path.split('/')
                val before = old?.path?.takeIf { samePath(it, left.path) }?.split('/')
                fun mine(i: Int) = before != null && ours[i] != theirs[i] && ours[i] != before[i] && theirs[i] == before[i]
                val target = ours.indices.map { if (mine(it)) ours[it] else theirs[it] }
                if (ours.last() != target.last()) synchronized(filesLock) {
                    val source = safePath(root, current(left).path)
                    val dest = File(source.parentFile, target.last())
                    if (exists(dest) && !sameFolder(dest, source)) return@synchronized
                    try {
                        changeCase(source, dest, move)
                        syncDirectory(source.parentFile!!)
                        status.changed = true
                    } catch (_: IOException) { return@synchronized } // old name kept; the next sync tries again
                    left = readRecord(root, (ours.dropLast(1) + target.last()).joinToString("/"))
                }
                if (ours.indices.any(::mine)) {
                    current(left)
                    right = call { client.move(left.id, target.joinToString("/"), right.path) }
                    status.changed = true
                }
            }
            var base = (old ?: Record(left.id, left.path, emptyMap(), right.tag)).copy(path = left.path, tag = right.tag)
            remember(base)
            for (name in FileName.entries) {
                val localStamp = left.files[name]; val remoteStamp = right.files[name]; val previousStamp = base.files[name]
                val chosen: Stamp?
                if (localStamp?.sha256 == remoteStamp?.sha256) chosen = remoteStamp
                else {
                    val localChanged = localStamp?.sha256 != previousStamp?.sha256
                    val remoteChanged = remoteStamp?.sha256 != previousStamp?.sha256
                    val upload = if (localChanged && remoteChanged) {
                        if (localStamp == null || remoteStamp == null || localStamp.mtimeNs == remoteStamp.mtimeNs) throw SyncException(tr("Both sides edited the file; which edit is newer is unknown"))
                        localStamp.mtimeNs > remoteStamp.mtimeNs
                    } else localChanged
                    val file = recordFile(root, left.path, name)
                    if (upload) {
                        current(left)
                        if (stamp(file) != localStamp) throw SyncException(tr("The local file has already changed"))
                        chosen = if (localStamp != null) {
                            val source = readFile(file)
                            if (source.stamp != localStamp) throw SyncException(tr("The local file has already changed"))
                            call { client.write(left.id, name, source.bytes, localStamp.mtimeNs, remoteStamp?.sha256) }
                        } else {
                            call { client.removeFile(left.id, name, checkNotNull(remoteStamp).sha256) }
                            null
                        }
                    } else {
                        val received = if (remoteStamp == null) null else call { client.read(left.id, name) }.also {
                            if (it.stamp != remoteStamp) throw SyncException(tr("The server file has already changed"))
                        }
                        synchronized(filesLock) {
                            current(left)
                            if (received != null) writeFile(file, received.bytes, received.stamp.mtimeNs, localStamp)
                            else {
                                if (stamp(file) != localStamp) throw SyncException(tr("The local file has already changed"))
                                Files.delete(file.toPath()); syncDirectory(file.parentFile!!)
                            }
                        }
                        chosen = remoteStamp
                    }
                    status.changed = true
                }
                base = base.copy(files = if (chosen == null) base.files - name else base.files + (name to chosen))
                remember(base)
            }
        }

        suspend fun sections() {
            val fresh = scanLocal(busy())
            if (!fresh.complete && !fresh.missingRoot) return
            val manifest = call { client.manifest() }
            remote(manifest) // the whole second reply is checked before the first section change
            val left = fresh.manifest.sections.toMutableSet()
            val right = manifest.sections.toMutableSet()
            val old = previous.sections.toMutableSet()
            fun variant(names: Set<String>, name: String) = names.firstOrNull { it != name && samePath(it, name) }
            // this device's names first: a section renamed by case here appears on the server before the old name goes
            for (name in (left + right + old).sortedWith(compareBy<String> { it !in left }.thenBy { it })) {
                if ('/' in name) throw SyncException(tr("The server returned a nested section"))
                val path = safePath(root, name)
                if (covered(fresh.blockedPaths, name)) continue
                selected()
                val there = if (name in left && name !in right) variant(right, name) else null
                val here = if (name in right && name !in left) variant(left, name) else null
                if (there != null) {
                    // the same section on the server in another case: changed here — the new name goes up, otherwise followServerCase handles it
                    if (!renamedHere(name, there)) continue
                    call { client.section(name) }; right += name
                    status.changed = true
                } else if (here != null) {
                    // the old server spelling goes once this device's is there and no recordings remain under the old one
                    if (name !in oldSections || here !in right || manifest.records.any { it.path.startsWith("$name/") }) continue
                    call { client.section(name, delete = true) }; right -= name
                    status.changed = true
                } else if (name in left && name !in right) {
                    if (name in old) {
                        if (path.list()?.isNotEmpty() != false) continue
                        Files.delete(path.toPath()); syncDirectory(path.parentFile!!)
                    } else { call { client.section(name) }; right += name }
                    status.changed = true
                } else if (name in right && name !in left) {
                    if (name in old) { call { client.section(name, delete = true) }; right -= name }
                    else { Files.createDirectories(path.toPath()); syncDirectory(path.parentFile!!); left += name }
                    status.changed = true
                }
                if (name in left && name in right) old += name else old -= name
                previous = previous.copy(sections = old.sorted()); save()
            }
        }
    }
}
