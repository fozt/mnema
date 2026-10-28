package dev.mnema.app.ui.record

import android.util.Log
import dev.mnema.app.AppGraph
import dev.mnema.app.helper.HelperCommand
import dev.mnema.app.helper.HelperStatus
import dev.mnema.app.i18n.tr
import dev.mnema.app.recordings.RecordingStore
import dev.mnema.app.recordings.recordingDirectory
import dev.mnema.app.recordings.recordingId
import java.io.File
import java.io.IOException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

internal val CANCEL_LABEL: String get() = tr("Cancel recording")
internal val CANCEL_TITLE: String get() = tr("Cancel recording?")
internal val CANCEL_TEXT: String get() = tr("The audio will not be saved.")
internal val KEEP_LABEL: String get() = tr("Keep recording")
internal val CANCEL_NOT_DELETED: String get() = tr("The recording stopped but was not deleted. Delete it in Recordings")

private const val LOG_TAG = "MnemaRecord"
private const val MARK = ".mnema-cancelled"
// the stop command waits 3 s itself; as long again for the helper to close its files
private const val STOP_WAIT_MS = 6_000L
private const val STOP_STEP_MS = 200L

sealed interface CancelOutcome {
    // the helper no longer records this one: nothing touched
    data object NotRecording : CancelOutcome
    // the command was not sent: recording goes on
    data object NotSent : CancelOutcome
    data object Deleted : CancelOutcome
    // the recording stays, no processing is queued for it
    data class NotDeleted(val reason: String?) : CancelOutcome
}

/** Cancelled recordings: a file in the folder survives a restart, process memory covers a file that was not created. */
class CancelMarks {
    private val marked = mutableMapOf<String, String?>()

    fun mark(dir: File, id: String?) {
        synchronized(marked) { marked[key(dir)] = id }
        try { File(dir, MARK).createNewFile() } catch (_: IOException) { /* the in-memory mark remains */ }
    }

    fun unmark(dir: File) {
        synchronized(marked) { marked.remove(key(dir)) }
        File(dir, MARK).delete()
    }

    fun forget(dir: File) {
        synchronized(marked) { marked.remove(key(dir)) }
    }

    /** A folder with the mark; from memory only the same recording, not a new one under the same name. */
    fun cancelled(dir: File): Boolean {
        if (File(dir, MARK).isFile) return true
        val path = key(dir)
        val (known, id) = synchronized(marked) { (path in marked) to marked[path] }
        return known && (id == null || id == recordingId(dir))
    }

    // /sdcard is a link: one recording under different paths
    private fun key(dir: File): String = try { dir.canonicalPath } catch (_: IOException) { dir.absolutePath }
}

/** What cancelling needs from outside: the helper heartbeat, the stop command (IOException: not sent) and the shared recording deletion. */
internal class CancelSteps(
    val root: File,
    val status: () -> HelperStatus?,
    val stop: suspend () -> Unit,
    val recordings: RecordingStore,
)

/**
 * The mark goes before stop so processing after the stop sees it. Only folder [folder], which the helper was writing
 * when cancelled, and only the same recording is deleted; there is no trash, it is forever, consent is already given.
 */
internal suspend fun cancelRecording(
    folder: String,
    steps: CancelSteps,
    marks: CancelMarks,
    waitMs: Long = STOP_WAIT_MS,
    stepMs: Long = STOP_STEP_MS,
): CancelOutcome {
    // the heartbeat is unreadable or of another protocol: nothing to stop it with
    val current = steps.status() ?: return CancelOutcome.NotSent
    if (!writes(current, folder)) return CancelOutcome.NotRecording
    val dir = recordingDirectory(steps.root, File(steps.root, folder)) ?: return CancelOutcome.NotRecording
    val id = recordingId(dir)
    marks.mark(dir, id)
    try {
        steps.stop()
    } catch (_: IOException) {
        marks.unmark(dir)
        return CancelOutcome.NotSent
    }
    withTimeoutOrNull(waitMs) { while (writes(steps.status(), folder)) delay(stepMs) }
    if (id != null && recordingId(dir) != id) return CancelOutcome.NotDeleted(null)
    if (!steps.recordings.delete(dir, forever = true)) return CancelOutcome.NotDeleted(steps.recordings.failure.value)
    marks.forget(dir)
    return CancelOutcome.Deleted
}

private fun writes(status: HelperStatus?, folder: String): Boolean =
    folder.isNotEmpty() && status != null && status.recording && status.folder == folder

/** Cancel from the Record screen and from the notification: one at a time, the outcome shows on the screen. */
object RecordingCancel {
    private val marks = CancelMarks()
    private val lock = Mutex()
    private val running = MutableStateFlow(false)
    private val trouble = MutableStateFlow<String?>(null)
    val busy: StateFlow<Boolean> = running.asStateFlow()
    val problem: StateFlow<String?> = trouble.asStateFlow()

    /** Check before moving to a section and before processing a stopped recording. */
    fun cancelled(dir: File): Boolean = marks.cancelled(dir)

    fun dismissProblem() {
        trouble.value = null
    }

    suspend fun cancel(graph: AppGraph, folder: String): CancelOutcome = lock.withLock {
        running.value = true
        trouble.value = null
        try {
            val outcome = helperClient(graph).fold(
                onSuccess = { client ->
                    val steps = CancelSteps(graph.recordingsRoot, client::readStatus, { client.command(HelperCommand.STOP) }, graph.recordings)
                    withContext(Dispatchers.IO) { cancelRecording(folder, steps, marks) }
                },
                onFailure = { CancelOutcome.NotSent },
            )
            trouble.value = when (outcome) {
                CancelOutcome.NotSent -> SEND_FAILED
                is CancelOutcome.NotDeleted -> CANCEL_NOT_DELETED
                else -> null
            }
            if (outcome !is CancelOutcome.Deleted && outcome !is CancelOutcome.NotRecording) Log.w(LOG_TAG, "Cancel of $folder: $outcome")
            outcome
        } finally {
            running.value = false
        }
    }
}
