package dev.mnema.app.processing

import dev.mnema.app.recordings.chosenTitle
import dev.mnema.app.recordings.outcomeKey
import dev.mnema.app.recordings.recordingId
import dev.mnema.app.transcript.Meta
import dev.mnema.app.sync.filesLock
import dev.mnema.app.transcript.atomicWrite
import java.io.File
import java.io.IOException
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/** A track being processed; [key] — `size:mtime`, another key makes the upload invalid. */
@Serializable
data class TrackState(
    val key: String = "",
    val upload: String? = null,
    val state: String = NEW,
    val uploadId: String? = null,
    val uploaded: Long = 0,
) {
    companion object {
        const val NEW = "new"
        const val UPLOADED = "uploaded"
    }
}

/**
 * The folder's file of the work in progress is `.mnema-android.json`: stage, job, uploads, own title; the processing outcome is in the
 * `.mnema-meta.json` history. [id] — the recording id in meta: a folder copy with a new id does not continue someone else's work.
 * [since] — the history mark ([outcomeKey]) the work started from or wrote itself: another one means an outcome was added without it.
 */
@Serializable
data class ProcessState(
    val id: String,
    val tracks: Map<String, TrackState> = emptyMap(),
    val stage: String = ProcessStage.NEW,
    val summaryOnly: Boolean = false,
    val started: String = "",
    val options: ReprocessOptions? = null,
    val steps: Map<String, Double> = emptyMap(),
    val node: String? = null,
    val key: String? = null,
    val job: String? = null,
    val sentName: String? = null,
    // a failure before the node result (the node did not answer, token, upload): the history doesn't have it, processing can go on
    val problem: String? = null,
    // failure — the node did not answer: the card offers to choose another one
    val unreachable: Boolean = false,
    // auto-retries of this run: the job again and uploads lost before it was accepted; a manual retry starts from scratch
    val retries: Int = 0,
    val resends: Int = 0,
    // own recording title: the processing window and "Set title…"; it names the folder at the end, not the server
    val userTitle: String? = null,
    // own title sent with the job: the server named the folder with it; if it was removed afterwards its name is not taken
    val sentTitle: String? = null,
    val since: String = "",
)

object ProcessStage {
    const val NEW = "new"
    const val ASR = "asr"
    const val SUMMARY = "summary"
    const val DONE = "done"
    const val FAILED = "failed"
    const val STOPPED = "stopped"

    // no server set: only the audio is saved, this is not a failure
    const val AUDIO_ONLY = "audio_only"

    /** Processing by the file is still going on: without WorkManager work nobody will continue it. */
    val UNFINISHED = setOf(NEW, ASR, SUMMARY)
}

object ProcessStateFile {
    const val FILE = ".mnema-android.json"

    private val json = Json {
        ignoreUnknownKeys = true
        encodeDefaults = true
    }

    /** null: no file, it is truncated or not ours (SerializationException is an IllegalArgumentException too). */
    fun read(dir: File): ProcessState? = try {
        json.decodeFromString(ProcessState.serializer(), File(dir, FILE).readText(Charsets.UTF_8))
    } catch (_: IOException) {
        null
    } catch (_: IllegalArgumentException) {
        null
    }

    fun write(dir: File, s: ProcessState) = synchronized(filesLock) {
        atomicWrite(File(dir, FILE), json.encodeToString(ProcessState.serializer(), s).toByteArray(Charsets.UTF_8))
    }

    /** This recording's work: the file id is the recording id in meta; a file copied with another folder belongs to someone else. */
    fun own(dir: File): ProcessState? = read(dir)?.takeIf { it.id == recordingId(dir) }

    /** Own work after which no outcome was added to the history: only it decides what happened to the recording. */
    fun current(dir: File): ProcessState? = own(dir)?.takeIf { it.since == outcomeKey(Meta.read(dir)) }
}

/** The title from the processing window becomes the own title [ProcessState.userTitle] by the chosenTitle rule; [part] — the folder part mark. */
internal fun withOwnTitle(state: ProcessState, part: String = ""): ProcessState {
    val title = state.options?.title ?: return state
    return state.copy(userTitle = chosenTitle(title, part), options = state.options.copy(title = null))
}

/**
 * A manual retry of a failure continues the same job, including an unknown POST outcome; auto-retries start anew, as on desktop.
 * [previous] — [ProcessStateFile.current] of recording [id], [since] — its history mark now: a work after which an outcome
 * was added without it is not continued.
 */
fun queued(previous: ProcessState?, summaryOnly: Boolean, options: ReprocessOptions? = null, id: String, since: String): ProcessState {
    val current = previous?.takeIf { it.id == id && it.since == since }
        ?: return withOwnTitle(ProcessState(id = id, summaryOnly = summaryOnly, options = options, since = since))
    if (options == null && current.summaryOnly == summaryOnly &&
        (current.stage == ProcessStage.FAILED || current.stage in ProcessStage.UNFINISHED)) {
        return current.copy(stage = ProcessStage.NEW, problem = null, unreachable = false, retries = 0, resends = 0)
    }
    // own title as in the processing window: without the window the previous one stays
    return withOwnTitle(current.copy(stage = ProcessStage.NEW, summaryOnly = summaryOnly, started = "",
        options = options, tracks = emptyMap(), steps = emptyMap(), key = null, job = null, sentName = null,
        problem = null, unreachable = false, retries = 0, resends = 0, userTitle = current.userTitle.takeIf { options == null }))
}

/** A stopped shared job is not used by the next run; the own title goes with it, as on desktop. */
fun stopped(state: ProcessState): ProcessState = state.copy(
    stage = if (state.stage in ProcessStage.UNFINISHED) ProcessStage.STOPPED else state.stage,
    key = null,
    job = null,
    sentName = null,
    userTitle = null,
)
