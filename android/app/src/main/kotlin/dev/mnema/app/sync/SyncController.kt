package dev.mnema.app.sync

import dev.mnema.app.i18n.tr
import dev.mnema.app.server.Connection
import java.io.File
import java.io.IOException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/** One instance for the worker and explicit bulk choices; statuses are not an operation queue. */
class SyncController(
    private val root: File,
    private val stateFile: File,
    private val selected: () -> Connection?,
    busy: () -> Set<String>,
    trash: (File) -> Unit,
    private val changed: () -> Unit,
    private val prepare: suspend () -> Unit = {},
    clientFactory: (Connection) -> LibraryClient = { LibraryClient(it) },
    hasTrash: () -> Boolean = { true },
) {
    private val engine = LibrarySync(root, stateFile, selected, busy, trash, clientFactory, hasTrash, log = { android.util.Log.i("MnemaSync", it) })
    private val lock = Mutex()
    private val shown = MutableStateFlow(SyncStatus())
    val status: StateFlow<SyncStatus> = shown.asStateFlow()
    private var shownConnection: Connection? = null

    // without the sync lock, so selecting a server does not wait for a running sync; creates the folder only before any sync
    fun initializeLibrary() = synchronized(filesLock) { initializeLibrary(root, stateFile) }

    /**
     * Checks a folder against the last successful sync with the selected server: the same id and texts. null without a
     * selected server: nobody confirms, a processed recording's audio can be deleted and the texts stay only on the phone.
     */
    fun textsOnServer(): ((File) -> Boolean)? {
        val server = selected()?.url ?: return null
        val state = loadCheckpoint(stateFile)
        return { dir -> textsOnServer(root, dir, state, server) }
    }

    /** Sections of the last sync: the feed never treats them as recordings, even with sync off. */
    fun knownSections(): Set<String> = try { loadCheckpoint(stateFile).sections.toSet() } catch (_: IOException) { emptySet() }

    /** A server is selected: sync separates copies with the same id. */
    fun enabled(): Boolean = selected() != null

    /** Path of recording [id] at the last sync: among copies with this id, the folder at that path keeps it. */
    fun previousPath(id: String): String? = try { loadCheckpoint(stateFile).records[id]?.path } catch (_: IOException) { null }

    suspend fun runOnce() = cycle { engine.runOnce() }
    // forever — "Delete forever" was chosen because there is no trash
    suspend fun resolveBulk(ids: List<String>, restore: Boolean, forever: Boolean = false) = cycle {
        if (selected() != shownConnection) SyncStatus(errors = mutableListOf(tr("The sync server changed; refresh the list")))
        else engine.resolveBulk(ids, restore, forever)
    }
    private suspend fun cycle(action: suspend () -> SyncStatus) = lock.withLock {
        val connection = selected()
        val result = try { prepare(); action() } catch (e: IOException) {
            SyncStatus(errors = mutableListOf(e.message ?: tr("Could not sync recordings with the server")))
        }
        shownConnection = connection
        publish(if (selected() == connection) result else SyncStatus(errors = mutableListOf(tr("The sync server changed; a new sync will start"))))
    }
    private fun publish(status: SyncStatus): SyncStatus {
        shown.value = status
        if (status.changed) changed()
        return status
    }
}

/** Only selecting a server (AppGraph) creates a new library before the first recording; the first sync does not create the folder. */
internal fun initializeLibrary(root: File, checkpoint: File) {
    try {
        java.nio.file.Files.readAttributes(checkpoint.toPath(), java.nio.file.attribute.BasicFileAttributes::class.java,
            java.nio.file.LinkOption.NOFOLLOW_LINKS)
    } catch (_: java.nio.file.NoSuchFileException) {
        java.nio.file.Files.createDirectories(root.toPath())
    }
}
