package dev.mnema.app

import android.content.Context
import android.os.SystemClock
import dev.mnema.app.i18n.AcceptLanguage
import dev.mnema.app.recordings.busyRecordings
import dev.mnema.app.recordings.moveToSection
import dev.mnema.app.recordings.recordingDirectory
import dev.mnema.app.recordings.recordingId
import dev.mnema.app.recordings.sectionGone
import dev.mnema.app.transcript.Meta
import dev.mnema.app.sync.NativeTrash
import dev.mnema.app.sync.SyncController
import dev.mnema.app.sync.SyncException
import dev.mnema.app.sync.SyncScheduling
import dev.mnema.app.sync.filesLock
import java.util.UUID
import java.util.TimeZone
import java.io.IOException
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.distinctUntilChanged
import dev.mnema.app.helper.HelperClient
import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.helper.HelperStatus
import dev.mnema.app.i18n.tr
import dev.mnema.app.launch.HelperLauncher
import dev.mnema.app.launch.KadbLauncher
import dev.mnema.app.processing.ProcessingScheduler
import dev.mnema.app.processing.WorkScheduler
import dev.mnema.app.recordings.FolderRecordingStore
import dev.mnema.app.recordings.ProcessingState
import dev.mnema.app.recordings.RecordingStore
import dev.mnema.app.server.Connection
import dev.mnema.app.server.ConnectionException
import dev.mnema.app.server.RunnerClient
import dev.mnema.app.server.parseConnection
import dev.mnema.app.settings.NodeEntry
import dev.mnema.app.settings.Settings
import dev.mnema.app.settings.SettingsStore
import dev.mnema.app.ui.record.RecordingCancel
import dev.mnema.app.update.UpdateChecker
import java.io.File
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient

/** Process-wide app dependencies: one instance of each. */
class AppGraph(val context: Context) {
    val settings = SettingsStore(context)
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    // the exchange folder may be unavailable: then the first access fails, not the start
    val helper: HelperClient by lazy { HelperClient.forContext(context, scope) }
    val launcher: HelperLauncher by lazy { KadbLauncher(context) }

    // no redirects: the token goes nowhere beyond the given address
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .followRedirects(false)
        .followSslRedirects(false)
        .build()
    private val runnerHttp = http.newBuilder().readTimeout(60, TimeUnit.SECONDS).writeTimeout(60, TimeUnit.SECONDS).addInterceptor(AcceptLanguage).build()

    // the registry serves the APK by a redirect to storage: only https → https, no tokens
    val updates: UpdateChecker by lazy {
        UpdateChecker(context, http.newBuilder().followRedirects(true).readTimeout(30, TimeUnit.SECONDS).build(), settings, scope, ::recordingFolder)
    }

    // /sdcard is a system symlink. Inside the library every component compares one real root.
    val recordingsRoot: File = File(HelperProtocol.RECORDINGS_ROOT).canonicalFile
    private val activeFolders = mutableMapOf<String, Int>()
    private val syncScheduling by lazy { SyncScheduling(context) }
    private val trash = NativeTrash(context, recordingsRoot)
    private val scheduler = WorkScheduler(context, ::recordingForMutation, ::wakeSync, prepare = { runBlocking { prepareMutation() } })
    val processing: ProcessingScheduler = scheduler
    private val store = FolderRecordingStore(recordingsRoot, scope, ::recordingNow, scheduler::hasWork,
        trash = trash::move, busy = ::busyFolders, changed = ::wakeSync, prepare = ::prepareMutation, textsOnServer = { sync.textsOnServer() },
        knownSections = { sync.knownSections() }, syncing = { sync.enabled() }, hasTrash = trash::available)
    val recordings: RecordingStore = store
    // recordings are synced with the same server that processes them
    val sync: SyncController by lazy {
        SyncController(recordingsRoot, File(context.filesDir, "library-sync.json"), selected = {
            runBlocking { settings.current().activeEntry() }?.let { node ->
                try { parseConnection(node.connection) } catch (_: ConnectionException) { null }
            }
        }, busy = ::busyFolders, trash = trash::move, changed = store::refresh, prepare = {
            if (settings.current().activeEntry() != null) prepareMutation()
        }, hasTrash = trash::available)
    }

    init {
        scope.launch(Dispatchers.IO) {
            try { scheduler.resume() } catch (e: IOException) { android.util.Log.w("MnemaWork", "Unfinished processing not read", e) }
        }
        scope.launch(Dispatchers.IO) { scheduler.observeCompletion() }
        scope.launch(Dispatchers.IO) {
            settings.settings.map { it.activeEntry()?.connection }.distinctUntilChanged().collect {
                // the selected server syncs recordings: a new library needs the folder before the first sync, after an update too
                if (it != null) openLibrary()
                syncScheduling.configure(it != null)
            }
        }
    }

    fun wakeSync() { syncScheduling.wake() }

    private fun openLibrary() {
        try { sync.initializeLibrary() } catch (e: IOException) { android.util.Log.w("MnemaSync", "Recordings folder unavailable", e) }
    }

    /** The Worker slot stays taken until finally, even if cancel has already written STOPPED. */
    internal fun processingStarted(dir: File) = synchronized(filesLock) {
        val held = setOfNotNull(dir.absolutePath, recordingId(dir))
        held.forEach { activeFolders[it] = (activeFolders[it] ?: 0) + 1 }
        held
    }

    internal fun processingFinished(held: Set<String>) = synchronized(filesLock) {
        held.forEach { key ->
            val count = (activeFolders[key] ?: 1) - 1
            if (count == 0) activeFolders.remove(key) else activeFolders[key] = count
        }
        store.refresh()
        wakeSync()
    }

    /** START comes both from the button and from automatic call recording. */
    internal suspend fun recordingStarted() = withContext(Dispatchers.IO) {
        synchronized(filesLock) {
            val status = helperOrNull()?.readStatus()?.takeIf { it.recording } ?: return@synchronized
            val dir = recordingDirectory(recordingsRoot, File(recordingsRoot, status.folder)) ?: return@synchronized
            Meta.ensureRecording(dir, UUID.randomUUID().toString()) {
                Meta.isoNow(status.startedMs.takeIf { it > 0 } ?: System.currentTimeMillis(), TimeZone.getDefault())
            }
        }
        store.refresh()
        wakeSync()
    }

    /**
     * A stopped recording goes to the section chosen on "Recordings" before processing is queued; if that fails it is processed in
     * place; cancelled — null. When the section is gone the recording stays in the root and the choice becomes "No section", as on desktop.
     */
    internal suspend fun recordingStopped(dir: File): File? = withContext(Dispatchers.IO) {
        if (RecordingCancel.cancelled(dir)) return@withContext null
        val section = settings.current().recordSection.ifEmpty { return@withContext dir }
        val known = sync.knownSections()
        var gone = false
        val moved = try {
            prepareMutation()
            // section, busy state and move under one lock: processing and sync won't take a folder halfway
            synchronized(filesLock) {
                gone = sectionGone(recordingsRoot, section, known)
                if (gone) dir else moveToSection(recordingsRoot, dir, section, busyFolders(), known)
            }
        } catch (e: IOException) {
            android.util.Log.w("MnemaRecord", "Recording ${dir.name} left without a section", e)
            dir
        }
        if (gone) {
            try { settings.update { if (it.recordSection == section) it.copy(recordSection = "") else it } }
            catch (e: IOException) { android.util.Log.w("MnemaRecord", "Section choice not reset", e) }
        }
        if (moved != dir) store.refresh()
        moved
    }

    private suspend fun prepareMutation() {
        val client = helperOrNull() ?: throw IOException(tr("Couldn't check the recording state"))
        client.prepareMutation()
    }

    private fun recordingForMutation(): String? {
        val client = helperOrNull() ?: throw IOException(tr("Couldn't check the recording state"))
        return client.recordingFolderForMutation()
    }

    private fun busyFolders(): Set<String> = synchronized(filesLock) {
        val recording = try { recordingForMutation() } catch (e: IOException) { throw SyncException(e.message.orEmpty()) }
        busyRecordings(recordingsRoot, recording, scheduler::hasWork, activeFolders.keys)
    }

    /** Client of the active node; null — no node is selected or its connection string is invalid. */
    fun runner(settings: Settings): RunnerClient? = settings.activeEntry()?.let(::runner)

    fun runner(node: NodeEntry): RunnerClient? = try {
        runner(parseConnection(node.connection))
    } catch (_: ConnectionException) {
        null
    }

    fun runner(connection: Connection): RunnerClient = RunnerClient(connection, runnerHttp) { name -> nodeNamed(connection.url, name) }

    // the node name in the list comes from the last successful capabilities answer, wherever it was asked
    private fun nodeNamed(address: String, name: String) {
        scope.launch(Dispatchers.IO) {
            try { settings.update { it.withNodeName(address, name) } }
            catch (e: IOException) { android.util.Log.w("MnemaNodes", "Node name not saved", e) }
        }
    }

    internal fun reportProgress(dir: File, state: ProcessingState?) = store.report(dir, state)

    /** The folder the helper writes to; a stale heartbeat — ask with ping, like HelperClient. */
    internal suspend fun recordingFolder(): String? {
        val client = helperOrNull() ?: return null
        val status = withContext(Dispatchers.IO) { client.readStatus() }?.takeIf { it.recording } ?: return null
        if (!fresh(status) && !client.ping()) return null
        return status.folder.ifEmpty { null }
    }

    private fun recordingNow(): String? {
        val status = helperOrNull()?.readStatus()?.takeIf { it.recording && fresh(it) } ?: return null
        return status.folder.ifEmpty { null }
    }

    private fun fresh(status: HelperStatus): Boolean =
        SystemClock.uptimeMillis() - status.heartbeatUptimeMs in 0 until HelperProtocol.ALIVE_MS

    private fun helperOrNull(): HelperClient? = try {
        helper
    } catch (_: IllegalStateException) {
        null
    }
}

/** What to hide in logs, remarks and notifications: the tokens of all nodes. */
internal fun secretsOf(settings: Settings): List<String> {
    val tokens = settings.nodes.flatMap { node ->
        val token = try {
            parseConnection(node.connection).token
        } catch (_: ConnectionException) {
            null
        }
        listOfNotNull(token, node.connection.substringAfter("#token=", "").ifEmpty { null })
    }
    return tokens
}
