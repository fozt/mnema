package dev.mnema.app.processing

import android.content.Context
import android.util.Log
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.WorkManager
import androidx.work.workDataOf
import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.i18n.tr
import dev.mnema.app.transcript.Meta
import dev.mnema.app.transcript.RecordingFolder
import dev.mnema.app.recordings.libraryFolders
import dev.mnema.app.recordings.outcomeKey
import dev.mnema.app.recordings.recordingDirectory
import dev.mnema.app.recordings.recordingId
import dev.mnema.app.recordings.startedIso
import dev.mnema.app.sync.keeperFirst
import dev.mnema.app.sync.filesLock
import dev.mnema.app.sync.renewIdentity
import java.io.File
import java.io.IOException
import java.util.TimeZone
import java.util.UUID
import java.util.concurrent.ExecutionException
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/** Processing a folder is the unique work "process-<id>": the recording id from `.mnema-meta.json`, it survives a rename. */
class WorkScheduler(private val context: Context, private val recordingNow: () -> String? = { null }, private val changed: () -> Unit = {}, private val prepare: () -> Unit = {}) : ProcessingScheduler {
    private val work: WorkManager get() = WorkManager.getInstance(context)
    private val root = File(HelperProtocol.RECORDINGS_ROOT)

    // on a user action: at once; an unreachable node returns an error with a manual retry
    override fun enqueue(dir: File, summaryOnly: Boolean): Boolean {
        // a full processing is already running — it goes on; "Summarize" starts anew
        val policy = if (summaryOnly) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.KEEP
        return schedule(dir, summaryOnly, policy, options = null)
    }

    // the user's choice: a running processing of this folder restarts with it, its server jobs are cancelled
    override fun reprocess(dir: File, options: ReprocessOptions): File? {
        try { prepare() } catch (_: IOException) { return null }
        return try { synchronized(filesLock) {
        if (!canMutate(dir)) return@synchronized null
        val (target, state) = replaceProcessing(root, dir, options, cancel = ::cancelOnServer) ?: return@synchronized null
        start(state.id, ExistingWorkPolicy.REPLACE)
        changed()
        target
        } } catch (_: IOException) { null }
    }

    override fun cancel(dir: File) {
        try {
            if (stopProcessing(root, dir, cancel = { state ->
                work.cancelUniqueWork(workName(state.id))
                cancelOnServer(state)
            })) changed()
        } catch (e: IOException) {
            Log.w(TAG, "Processing stop not written for ${dir.name}", e)
        }
    }

    private fun cancelOnServer(state: ProcessState) {
        val job = state.job ?: return
        val request = OneTimeWorkRequestBuilder<RunnerCancelWorker>()
            .setInputData(workDataOf(RunnerCancelWorker.KEY_JOB to job, RunnerCancelWorker.KEY_NODE to state.node))
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        work.enqueue(request)
    }

    /** The next step of the same processing; the step itself checks whether the node is reachable. */
    fun next(id: String, delayMs: Long) {
        val request = OneTimeWorkRequestBuilder<ProcessWorker>()
            .setInputData(workDataOf(ProcessWorker.KEY_ID to id))
            .setInitialDelay(delayMs, TimeUnit.MILLISECONDS)
            .build()
        work.enqueueUniqueWork(workName(id), ExistingWorkPolicy.APPEND_OR_REPLACE, request)
    }

    /** Whether the processing has unfinished work; if WorkManager did not answer, assume it has. */
    fun hasWork(id: String): Boolean = try {
        work.getWorkInfosForUniqueWork(workName(id)).get().any { !it.state.isFinished }
    } catch (e: ExecutionException) {
        Log.w(TAG, "Processing works of $id not read", e)
        true
    } catch (_: InterruptedException) {
        Thread.currentThread().interrupt()
        true
    }

    /** After return of a Worker, WorkManager removes the last busy flag; then a fresh sync is needed. */
    suspend fun observeCompletion() {
        work.getWorkInfosByTagFlow(ProcessWorker::class.java.name)
            .map { infos -> infos.filter { it.state.isFinished }.associate { it.id to it.state } }
            .distinctUntilChanged()
            .collect { changed() }
    }

    /** Processing abandoned without work (reinstall, crash) is queued again. */
    fun resume() {
        for (dir in orphaned(root, ::hasWork)) {
            val state = ProcessStateFile.current(dir) ?: continue
            start(state.id, ExistingWorkPolicy.KEEP)
        }
    }

    private fun schedule(dir: File, summaryOnly: Boolean, policy: ExistingWorkPolicy, options: ReprocessOptions?): Boolean {
        try { prepare() } catch (_: IOException) { return false }
        return try { synchronized(filesLock) {
        if (!accepts(dir, root, summaryOnly) || !canMutate(dir)) {
            Log.w(TAG, "Not a recording folder: ${dir.name}")
            return@synchronized false
        }
        val state = try {
            Meta.ensureRecording(dir, UUID.randomUUID().toString()) { startedIso(dir) }
            val id = recordingId(dir) ?: throw IOException(tr("The recording has no id"))
            queued(ProcessStateFile.read(dir), summaryOnly, options, id, outcomeKey(Meta.read(dir))).also { ProcessStateFile.write(dir, it) }
        } catch (e: IOException) {
            Log.w(TAG, "Processing state not written for ${dir.name}", e)
            return@synchronized false
        }
        start(state.id, policy)
        changed()
        true
        } } catch (_: IOException) { false }
    }

    private fun canMutate(dir: File): Boolean = try {
        recordingNow()?.let { File(root, it).canonicalFile == dir.canonicalFile } != true
    } catch (_: IOException) { false }

    private fun start(id: String, policy: ExistingWorkPolicy) {
        val request = OneTimeWorkRequestBuilder<ProcessWorker>()
            .setInputData(workDataOf(ProcessWorker.KEY_ID to id))
            .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
            .build()
        work.enqueueUniqueWork(workName(id), policy, request)
    }

    companion object {
        private const val TAG = "MnemaWork"

        fun workName(id: String) = "process-$id"

        /**
         * Folder of recording [id] in the root or a section. A folder copied mid-processing is a copy by the common copy rule
         * (keeperFirst): it gets a new id and its copy of the work file belongs to someone else; the original goes on. With
         * sync ([syncing]) the sync separates copies: ids don't change here, the work goes on in the folder the sync leaves the
         * id with ([previous] — the recording path at the previous sync).
         */
        fun findFolder(root: File, id: String, syncing: Boolean = false, previous: String? = null): File? = synchronized(filesLock) {
            val same = folders(root).filter { recordingId(it) == id }
            if (same.size < 2) return@synchronized same.singleOrNull()
            val base = root.canonicalFile
            val ordered = try { keeperFirst(same, previous.takeIf { syncing }, { it.relativeTo(base).invariantSeparatorsPath }) { it } }
                catch (_: IOException) { return@synchronized null }
            if (!syncing) for (copy in ordered.drop(1)) {
                try { renewIdentity(copy) } catch (_: IOException) { return@synchronized null } catch (_: IllegalArgumentException) { return@synchronized null }
            }
            ordered.first()
        }

        /** What the scheduler accepts: processing needs a track, a summary a non-empty transcript. */
        fun accepts(dir: File, root: File, summaryOnly: Boolean): Boolean {
            if (!summaryOnly) return RecordingFolder.isCandidate(dir, root, recordingNow = null)
            return try {
                val canonical = recordingDirectory(root, dir) ?: return false
                val transcript = File(canonical, RecordingFolder.TRANSCRIPT)
                transcript.isFile && transcript.readText(Charsets.UTF_8).isNotBlank()
            } catch (_: IOException) {
                false
            }
        }

        /** Folders whose own processing by the file is unfinished and has no work. */
        fun orphaned(root: File, hasWork: (String) -> Boolean): List<File> = folders(root).filter { dir ->
            val state = ProcessStateFile.current(dir)
            state != null && state.stage in ProcessStage.UNFINISHED && !hasWork(state.id)
        }

        private fun folders(root: File): List<File> =
            libraryFolders(root).records
    }
}

/** A new run with a chosen range: the previous server job of this folder is cancelled, not left hanging; another's work file cancels nothing. */
internal fun replaceProcessing(root: File, dir: File, options: ReprocessOptions, cancel: (ProcessState) -> Unit,
                               newId: () -> String = { UUID.randomUUID().toString() }): Pair<File, ProcessState>? = synchronized(filesLock) {
    val previous = ProcessStateFile.own(dir)?.takeIf { (it.stage == ProcessStage.FAILED || it.stage in ProcessStage.UNFINISHED) &&
        it.job != null }
    val target = reprocessed(dir, root, options, newId) ?: return@synchronized null
    val state = ProcessStateFile.own(target) ?: return@synchronized null
    if (previous != null) {
        cancel(previous)
        // the part went to a sibling folder: the source no longer waits for the cancelled job
        if (target != dir) ProcessStateFile.write(dir, stopped(previous))
    }
    target to state
}

/** "Stop processing": the node job is cancelled, the history gets a stopped processing; another's work file cancels nothing. */
internal fun stopProcessing(root: File, dir: File, cancel: (ProcessState) -> Unit, now: () -> Long = System::currentTimeMillis): Boolean =
    synchronized(filesLock) {
        if (recordingDirectory(root, dir) == null) return@synchronized false
        val state = ProcessStateFile.own(dir) ?: return@synchronized false
        cancel(state)
        val next = stopped(state)
        if (next == state) return@synchronized false
        // as on desktop: a stopped summary does not change the history
        if (state.stage in ProcessStage.UNFINISHED && !state.summaryOnly) Meta.appendRun(dir, state.stoppedRun(now()))
        ProcessStateFile.write(dir, next.copy(since = outcomeKey(Meta.read(dir))))
        true
    }

/** A stopped processing in the history: with no outcome before it the recording is "audio only", otherwise that outcome stays. */
private fun ProcessState.stoppedRun(nowMs: Long): JsonObject {
    val finished = Meta.isoNow(nowMs, TimeZone.getDefault())
    return JsonObject(mapOf(
        "kind" to JsonPrimitive("process"),
        "started" to JsonPrimitive(started.ifEmpty { finished }),
        "finished" to JsonPrimitive(finished),
        "status" to JsonPrimitive("stopped"),
    ))
}
