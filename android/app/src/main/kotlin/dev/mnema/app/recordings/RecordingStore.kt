package dev.mnema.app.recordings

import dev.mnema.app.model.DialogLine
import dev.mnema.app.model.Summary
import dev.mnema.app.processing.ReprocessOptions
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.MutableStateFlow
import java.io.File

sealed interface ProcessingState {
    data object Unprocessed : ProcessingState
    data object WaitingNetwork : ProcessingState
    // before recognition: repair, silence check, compression before upload
    data object Preparing : ProcessingState
    // audio is uploading: [progress] — the share of bytes sent
    data class Uploading(val progress: Double?) : ProcessingState
    // the audio is on the server, the job waits in its queue
    data object Queued : ProcessingState
    data class Recognizing(val progress: Double?, val message: String) : ProcessingState
    data object Summarizing : ProcessingState
    data object Done : ProcessingState
    // processing finished, the recording has no speech
    data object NoSpeech : ProcessingState
    data class NoSummary(val message: String) : ProcessingState
    data class AudioOnly(val message: String) : ProcessingState
    data class Failed(val message: String) : ProcessingState
}

/** Processing is running, from waiting for the network to the summary; a new state must go here or to the outcomes. */
val ProcessingState.inProgress: Boolean get() = when (this) {
    ProcessingState.WaitingNetwork, ProcessingState.Preparing, is ProcessingState.Uploading, ProcessingState.Queued,
    is ProcessingState.Recognizing, ProcessingState.Summarizing -> true
    ProcessingState.Unprocessed, ProcessingState.Done, ProcessingState.NoSpeech, is ProcessingState.NoSummary,
    is ProcessingState.AudioOnly, is ProcessingState.Failed -> false
}

data class RecordingItem(
    val dir: File,
    val title: String,
    val startedAt: Long,
    val durationS: Double,
    val about: String,
    val state: ProcessingState,
    val recordingNow: Boolean,
    val timeTitle: Boolean = false,
    val partLabel: String? = null,
    val id: String? = null,
    val section: String? = null,
    // the user's title that processing will give the folder at the end, while it runs
    val pendingTitle: String? = null,
    // warning from the last processing: the other side's voice was not recorded
    val warning: String? = null,
)

data class RecordingDetail(
    val item: RecordingItem,
    val summary: Summary?,
    val dialog: List<DialogLine>,
    // non-empty mic.wav and system.wav: the player plays them together
    val tracks: List<File>,
    val remarks: List<String>,
    // whether the scheduler accepts processing and a summary: the buttons use the same check
    val processable: Boolean,
    val summarizable: Boolean,
    val retrySummaryOnly: Boolean = false,
    val cleanable: Boolean = false,
    // the node did not respond: besides retrying, another one can be chosen
    val nodeDown: Boolean = false,
    // processing failed: an unchanged retry continues its job on the server
    val retryContinues: Boolean = false,
    // the choices of the failed run's dialog; null — the run was automatic
    val failedRun: ReprocessOptions? = null,
    // the helper's recording report exists and can be shared
    val report: Boolean = false,
)

/** A recording whose audio the processed-audio cleanup removes; [id] is checked again before trashing. */
data class CleanupRecord(val dir: File, val id: String, val bytes: Long)

// [trash] — whether the trash existed when counting: a confirmed plan without it deletes the audio forever;
// [onServer] — the selected server confirmed the texts; false — no server, the texts stay only on the phone
data class AudioCleanup(val records: List<CleanupRecord>, val trash: Boolean = true, val onServer: Boolean = true) {
    val bytes: Long get() = records.sumOf { it.bytes }
}

interface RecordingStore {
    val sections: StateFlow<List<String>> get() = NO_SECTIONS
    val failure: StateFlow<String?> get() = NO_FAILURE
    val items: StateFlow<List<RecordingItem>>
    // the first scan of the folders is over: until then an empty [items] means "not read yet", not "no recordings"
    val loaded: StateFlow<Boolean>
    // whether there is a trash; without it the delete dialogs ask about deleting forever
    val trashAvailable: StateFlow<Boolean> get() = WITH_TRASH
    fun refresh()
    suspend fun detail(dir: File): RecordingDetail?
    // forever — the user agreed to delete without the trash
    suspend fun clearAudio(dir: File, forever: Boolean = false): Boolean = false
    // null — the recordings could not be checked, the reason is in [failure]
    suspend fun audioCleanup(): AudioCleanup? = null
    suspend fun clearAudio(cleanup: AudioCleanup): Int = 0
    suspend fun delete(dir: File, forever: Boolean = false): Boolean
    // the new recording folder; null — failed, the reason is in [failure]
    suspend fun rename(dir: File, title: String): File? = null
    // "Move to section": empty — no section, create — "New section…"; the new recording folder, null — the reason is in [failure]
    suspend fun move(dir: File, section: String, create: Boolean = false): File? = null
}

private val NO_SECTIONS = MutableStateFlow<List<String>>(emptyList())
private val NO_FAILURE = MutableStateFlow<String?>(null)
private val WITH_TRASH = MutableStateFlow(true)
