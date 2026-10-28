package dev.mnema.app.recordings

import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.i18n.tr
import dev.mnema.app.processing.ProcessStage
import dev.mnema.app.processing.ProcessState
import dev.mnema.app.processing.ProcessStateFile
import dev.mnema.app.processing.ReprocessOptions
import dev.mnema.app.processing.TITLE_NOT_APPLIED
import dev.mnema.app.processing.WorkScheduler
import dev.mnema.app.transcript.Meta
import dev.mnema.app.transcript.RecordingFolder
import dev.mnema.app.transcript.Wav
import dev.mnema.app.sync.NO_TRASH
import dev.mnema.app.sync.keeperFirst
import dev.mnema.app.sync.discard
import dev.mnema.app.sync.filesLock
import dev.mnema.app.sync.renewIdentity
import dev.mnema.app.sync.restoreRenames
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.time.ZoneId
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import kotlin.coroutines.CoroutineContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/** Recordings are folders in `Documents/Mnema`; everything about a recording comes from its files, like the desktop feed. */
class FolderRecordingStore(
    private val root: File,
    private val scope: CoroutineScope,
    private val recordingNow: () -> String?,
    private val hasWork: (id: String) -> Boolean,
    private val io: CoroutineContext = Dispatchers.IO,
    private val trash: (File) -> Unit = { throw IOException(NO_TRASH) },
    private val busy: () -> Set<String> = { busyRecordings(root, recordingNow(), hasWork) },
    private val changed: () -> Unit = {},
    private val prepare: suspend () -> Unit = {},
    // the folder texts were confirmed by the last sync with the selected server (SyncController.textsOnServer); null — no server selected
    private val textsOnServer: () -> ((File) -> Boolean)? = { null },
    // sections of the last sync: stray files in them do not turn a section into a recording
    private val knownSections: () -> Set<String> = { emptySet() },
    // a server is selected: sync separates copies with the same id
    private val syncing: () -> Boolean = { false },
    // without the trash a recording is deleted forever, only after a separate confirmation
    private val hasTrash: () -> Boolean = { true },
) : RecordingStore {
    private val scanned = MutableStateFlow<List<RecordingItem>>(emptyList())
    private val live = MutableStateFlow<Map<File, ProcessingState>>(emptyMap())
    // progress of the last step, so the card keeps the upload and server share between steps
    private val lastStep = ConcurrentHashMap<File, ProcessingState>()
    private val shown = MutableStateFlow<List<RecordingItem>>(emptyList())
    private val scanning = Mutex()
    private val groups = MutableStateFlow<List<String>>(emptyList())
    private val problem = MutableStateFlow<String?>(null)
    private val bin = MutableStateFlow(true)
    override val trashAvailable: StateFlow<Boolean> = bin.asStateFlow()
    override val sections: StateFlow<List<String>> = groups.asStateFlow()
    override val failure: StateFlow<String?> = problem.asStateFlow()

    override val items: StateFlow<List<RecordingItem>> = shown.asStateFlow()

    override fun refresh() = refresh(clearFailure = true)

    private fun refresh(clearFailure: Boolean) {
        scope.launch {
            scanning.withLock {
                try {
                    scanned.value = withContext(io) { scan() }
                    if (clearFailure) problem.value = null
                    publish()
                } catch (e: IOException) { problem.value = e.message ?: tr("Could not read the recordings") }
            }
        }
    }

    /** Processing progress from work in this process; null — back to the folder files. */
    fun report(dir: File, state: ProcessingState?) {
        val key = dir.absoluteFile
        if (state != null) lastStep[key] = state
        live.update { if (state == null) it - key else it + (key to state) }
        publish()
    }

    override suspend fun detail(dir: File): RecordingDetail? = withContext(io) {
        try { prepare() } catch (_: IOException) { /* the card is shown, file actions are refused */ }
        val safe = recordingDirectory(root, dir)?.takeIf { isLibraryRecord(root, it, knownSections()) } ?: return@withContext null
        synchronized(filesLock) { ensureIdentity(safe) }
        val item = item(safe) ?: return@withContext null
        checkTrash()
        val summary = read(File(dir, RecordingFolder.SUMMARY))?.let(::parseSummaryFile)
        val tracks = TRACKS.map { File(dir, it) }.filter { it.isFile && Wav.seconds(it) > 0 }
        val dialog = read(File(dir, RecordingFolder.TRANSCRIPT))?.let(::parseDialog).orEmpty()
        val shownItem = withLive(item)
        // own work after a failure: a retry continues the same job; a failure before the node's result is not in history
        val work = ProcessStateFile.current(dir)
        val paused = paused(work)
        val failed = work?.takeIf { it.stage == ProcessStage.FAILED && !it.summaryOnly }
        RecordingDetail(
            item = shownItem,
            summary = summary,
            dialog = dialog,
            tracks = tracks,
            // a new processing run is going: the previous remarks do not apply
            remarks = if (shownItem.state.inProgress) emptyList() else recordingRemarks(Meta.read(dir), paused?.problem),
            processable = WorkScheduler.accepts(dir, root, summaryOnly = false),
            summarizable = WorkScheduler.accepts(dir, root, summaryOnly = true),
            retrySummaryOnly = retrySummary(dir, paused),
            cleanable = canClearAudio(safe),
            nodeDown = failed?.unreachable == true,
            retryContinues = failed != null,
            // the retry dialog opens with the failed run's choices and its user title
            failedRun = failed?.takeIf { it.options != null || it.userTitle != null }
                ?.let { (it.options ?: ReprocessOptions()).copy(title = it.userTitle) },
            report = File(dir, HelperProtocol.REPORT).isFile,
        )
    }

    /** The same trash for the UI and sync, rechecked under the lock; without it — forever, if [forever]. */
    override suspend fun delete(dir: File, forever: Boolean): Boolean = mutate(dir) { discard(it, forever, checkTrash(), trash) }

    /**
     * A recording being written is not renamed; one being processed is named by processing when it ends. Checked under
     * the same lock as the move.
     */
    override suspend fun rename(dir: File, title: String): File? {
        var renamed: File? = null
        val done = mutate(dir, whileProcessing = { safe -> nameProcessing(safe, title); renamed = safe }) {
            renamed = renameRecording(it, title)
        }
        return if (done) renamed else null
    }

    /** Recordings being written or processed are not moved; busyness is checked under the same lock as the move. */
    override suspend fun move(dir: File, section: String, create: Boolean): File? {
        var moved: File? = null
        val done = mutate(dir) { moved = moveRecording(root, it, section, knownSections(), create) }
        return if (done) moved else null
    }

    /** The audio of one recording, by the same rule as the processed-audio cleanup. */
    override suspend fun clearAudio(dir: File, forever: Boolean): Boolean = mutate(dir) { safe ->
        val record = cleanable(safe, busy(), textsOnServer()) ?: throw IOException(NOT_CLEANABLE)
        val withTrash = checkTrash()
        soundFiles(record.dir).orEmpty().forEach { discard(it, forever, withTrash, trash) }
    }

    /** Processed recordings with audio; with a selected server — those whose texts it has confirmed. */
    override suspend fun audioCleanup(): AudioCleanup? = withContext(io) {
        try {
            prepare()
            // one snapshot of the choice, so the list and the question about where texts stay refer to one server
            val onServer = textsOnServer()
            val active = busy()
            AudioCleanup(libraryFolders(root, knownSections()).records.mapNotNull { cleanable(it, active, onServer) }, checkTrash(), onServer != null)
        } catch (e: IOException) {
            problem.value = e.message ?: CLEANUP_FAILED
            null
        }
    }

    /** Each recording is rechecked under the lock: a busy, changed or replaced one keeps its audio. */
    override suspend fun clearAudio(cleanup: AudioCleanup): Int {
        val errors = mutableListOf<String>()
        val cleared = withContext(io) {
            try {
                prepare()
                val onServer = textsOnServer()
                val withTrash = checkTrash()
                // the trash vanished after counting: the user confirmed trashing, so nothing is deleted forever
                if (!withTrash && cleanup.trash) throw IOException(NO_TRASH)
                synchronized(filesLock) {
                    val active = busy()
                    cleanup.records.count { planned ->
                        val current = recordingDirectory(root, planned.dir)?.let { cleanable(it, active, onServer) }
                        if (current == null || current.id != planned.id) return@count false
                        try {
                            soundFiles(current.dir).orEmpty().forEach { discard(it, !cleanup.trash, withTrash, trash) }
                            true
                        } catch (e: IOException) {
                            errors += e.message ?: CLEANUP_FAILED
                            false
                        }
                    }
                }
            } catch (e: IOException) {
                errors += e.message ?: CLEANUP_FAILED
                0
            }
        }
        problem.value = errors.distinct().joinToString("\n").ifEmpty { null }
        refresh(clearFailure = false)
        if (cleared > 0) changed()
        return cleared
    }

    // the desktop rule (fixture audio-cleanup-v1) for the cleanup and for one recording's audio: processed by history, the last
    // run, stopped ones included, finished, no new run going, with a selected server the texts are confirmed by sync; audio is plain files
    private fun cleanable(dir: File, active: Set<String>, onServer: ((File) -> Boolean)?): CleanupRecord? {
        if (isBusy(dir, active)) return null
        val state = item(dir)?.state
        if ((state != ProcessingState.Done && state !is ProcessingState.NoSummary) || !processed(Meta.read(dir))) return null
        val sound = soundFiles(dir)?.takeIf { it.isNotEmpty() } ?: return null
        if (onServer != null && !onServer(dir)) return null
        val id = recordingId(dir) ?: return null
        return CleanupRecord(dir, id, sound.sumOf { it.length() })
    }

    private fun soundFiles(dir: File): List<File>? {
        val sound = (dir.listFiles() ?: return null).filter { it.extension.lowercase() in AUDIO_EXTENSIONS }
        return sound.takeIf { files -> files.none { Files.isSymbolicLink(it.toPath()) || !it.isFile } }
    }

    private suspend fun mutate(dir: File, whileProcessing: ((File) -> Unit)? = null, action: (File) -> Unit): Boolean {
        val result = withContext(io) {
            try { prepare() } catch (e: IOException) { problem.value = e.message; return@withContext false }
            synchronized(filesLock) {
                try {
                    val safe = recordingDirectory(root, dir)?.takeIf { isLibraryRecord(root, it, knownSections()) }
                        ?: throw IOException(tr("The recording folder is unavailable"))
                    when {
                        !isBusy(safe) -> action(safe)
                        whileProcessing != null && !isWriting(safe) -> whileProcessing(safe)
                        else -> throw IOException(tr("The recording is busy; wait for it to finish"))
                    }
                    problem.value = null
                    true
                } catch (e: IOException) { problem.value = e.message ?: tr("Could not change the recording"); false }
            }
        }
        // a partial audio cleanup must reach the player at once; the current error goes back to the calling UI
        refresh(clearFailure = false)
        if (result) changed()
        return result
    }

    private fun checkTrash(): Boolean = hasTrash().also { bin.value = it }

    private fun isBusy(dir: File, active: Set<String> = busy()): Boolean =
        dir.absolutePath in active || dir.relativeTo(root.canonicalFile).invariantSeparatorsPath in active || recordingId(dir) in active

    private fun isWriting(dir: File): Boolean = recordingNow().let { writing ->
        writing != null && (writing == dir.absolutePath || writing == dir.relativeTo(root.canonicalFile).invariantSeparatorsPath)
    }

    private fun canClearAudio(dir: File): Boolean = try {
        cleanable(dir, busy(), textsOnServer()) != null
    } catch (_: IOException) { false }

    private fun publish() {
        shown.value = scanned.value.map(::withLive)
    }

    private fun withLive(item: RecordingItem): RecordingItem =
        live.value[item.dir.absoluteFile]?.takeIf { !item.recordingNow }?.let { item.copy(state = it) } ?: item

    private fun scan(): List<RecordingItem> {
        checkTrash()
        var layout = libraryFolders(root, knownSections())
        // an interrupted case change: the recording returns under its name instead of vanishing from the list
        if (layout.renaming.isNotEmpty()) {
            val active = try { busy() } catch (_: IOException) { null }
            if (active != null && restoreRenames(layout.renaming, active)) layout = libraryFolders(root, knownSections())
        }
        groups.value = layout.sections
        layout.records.forEach { dir -> synchronized(filesLock) { ensureIdentity(dir) } }
        if (!syncing()) separateCopies(layout.records)
        return layout.records.mapNotNull(::item).sortedByDescending { it.startedAt }
    }

    /** Like the desktop: a folder copy with the same id becomes a new recording; the folder created first keeps the id. */
    private fun separateCopies(records: List<File>) = synchronized(filesLock) {
        val copies = records.groupBy(::recordingId).filter { (id, group) -> id != null && group.size > 1 }.values
        if (copies.isEmpty()) return@synchronized
        val active = try { busy() } catch (_: IOException) { return@synchronized }
        val base = root.canonicalFile
        for (group in copies) {
            val ordered = try { keeperFirst(group, null, { it.relativeTo(base).invariantSeparatorsPath }) { it } } catch (_: IOException) { continue }
            for (copy in ordered.drop(1)) {
                try { if (!isBusy(copy, active)) renewIdentity(copy) }
                catch (_: IOException) { /* the ids stay equal: an action will not pick a recording at random */ }
                catch (_: IllegalArgumentException) { /* meta is unreadable: the copy waits for the next scan */ }
            }
        }
    }

    // only for folders the library rule accepts as recordings, so meta is never written into a section
    private fun ensureIdentity(dir: File) {
        if (recordingId(dir) != null) return
        val occupied = try { isBusy(dir) } catch (_: IOException) { true }
        if (!occupied) Meta.ensureRecording(dir, UUID.randomUUID().toString()) { startedIso(dir) }
    }

    private fun item(dir: File): RecordingItem? {
        val files = dir.listFiles()?.filter { it.isFile } ?: return null
        val names = files.map { it.name }.toSet()
        // like the desktop _card: audio of any format or a text makes a card
        val audio = files.filter { it.extension.lowercase() in AUDIO_EXTENSIONS }
        if (audio.isEmpty() && RecordingFolder.TRANSCRIPT !in names && RecordingFolder.SUMMARY !in names) return null
        val seconds = audio.filter { it.extension.lowercase() == "wav" }.maxOfOrNull(Wav::seconds) ?: 0.0
        val summary = if (RecordingFolder.SUMMARY in names) read(File(dir, RecordingFolder.SUMMARY))?.let(::parseSummaryFile) else null
        val writing = recordingNow()
        val relative = dir.relativeTo(root.canonicalFile).invariantSeparatorsPath
        val recording = relative == writing || dir.absolutePath == writing
        val zone = ZoneId.systemDefault()
        val start = recordingStart(dir, zone)
        val shown = displayTitle(dir.name)
        val meta = Meta.read(dir)
        val id = meta?.string("id")
        // own work: the work file id is the recording id (ProcessStateFile.own)
        val process = if (ProcessStateFile.FILE in names) ProcessStateFile.read(dir)?.takeIf { it.id == id } else null
        val running = process?.takeIf { it.stage in ProcessStage.UNFINISHED && hasWork(it.id) }
        return RecordingItem(
            dir = dir,
            title = shown.title ?: timeText(start, shown.part),
            startedAt = start.at.atZone(zone).toInstant().toEpochMilli(),
            durationS = seconds,
            about = summary?.about.orEmpty(),
            state = if (recording) ProcessingState.Unprocessed else stateOf(dir, files, meta, process, running),
            recordingNow = recording,
            timeTitle = shown.title == null,
            partLabel = shown.part.removeSurrounding(" (", ")").ifEmpty { null },
            id = id,
            section = relative.substringBeforeLast('/', "").ifEmpty { null },
            // the user title names the folder when processing ends; until then it is a caption
            pendingTitle = running?.userTitle,
            warning = otherSideWarning(meta),
        )
    }

    /**
     * Recording state by the shared rule [recordState]: the outcome comes from history; own work is running (per
     * WorkManager), interrupted, or stopped on a failure before the node's result ([paused]); after an outcome written
     * without it, own work does not count.
     */
    private fun stateOf(dir: File, files: List<File>, meta: JsonObject?, process: ProcessState?, running: ProcessState?): ProcessingState {
        if (running != null) return when {
            running.summaryOnly || running.stage == ProcessStage.SUMMARY -> ProcessingState.Summarizing
            // a job on the server: an unchanged retry continues it even from stage NEW
            running.job != null -> lastStep[dir.absoluteFile]?.takeIf { it is ProcessingState.Recognizing || it == ProcessingState.Queued }
                ?: ProcessingState.Recognizing(null, "")
            running.stage == ProcessStage.NEW -> ProcessingState.Preparing
            else -> sending(dir, running)
        }
        val current = process?.takeIf { it.since == outcomeKey(meta) }
        paused(current)?.let { paused ->
            val problem = paused.problem.orEmpty()
            return if (paused.stage == ProcessStage.AUDIO_ONLY) ProcessingState.AudioOnly(problem) else ProcessingState.Failed(problem)
        }
        val names = files.map { it.name }.toSet()
        val work = if (current?.stage in ProcessStage.UNFINISHED) Work.INTERRUPTED else Work.NONE
        return when (recordState(files.associate { it.name to it.length() }, meta, work)) {
            RecordState.UNPROCESSED, RecordState.PROCESSING -> ProcessingState.Unprocessed
            RecordState.PROCESSED -> if (RecordingFolder.SUMMARY in names) ProcessingState.Done
                else ProcessingState.NoSummary(recordingRemarks(meta, null).firstOrNull().orEmpty())
            RecordState.NO_SPEECH -> ProcessingState.NoSpeech
            RecordState.AUDIO_ONLY -> ProcessingState.AudioOnly(STOPPED)
            RecordState.FAILED -> ProcessingState.Failed(outcome(meta)?.string("error") ?: FAILED)
        }
    }

    // no job yet: the audio is being compressed or uploaded
    private fun sending(dir: File, state: ProcessState): ProcessingState = when (val last = lastStep[dir.absoluteFile]) {
        is ProcessingState.Uploading, ProcessingState.Preparing -> last
        else -> if (state.tracks.values.any { it.upload != null || it.uploadId != null }) ProcessingState.Uploading(null) else ProcessingState.Preparing
    }

    private companion object {
        val TRACKS = listOf("mic.wav", "system.wav")
        val FAILED: String get() = tr("Processing failed")
        val CLEANUP_FAILED: String get() = tr("Could not delete the audio")
        val NOT_CLEANABLE: String get() = tr("The audio of this recording can't be deleted yet")
        val STOPPED: String get() = tr("Processing stopped; only the audio was saved")
    }
}

// a start-time name shows the start time as its title: "8:35 (0.10–1.35)"
private fun timeText(start: RecordingStart, part: String): String =
    "${start.at.hour}:${start.at.minute.toString().padStart(2, '0')}$part"

/** Recording title for notifications: the folder name without the date, or the start time for a start-time name. */
fun recordingTitle(dir: File): String = displayTitle(dir.name).let { it.title ?: timeText(recordingStart(dir), it.part) }

private fun JsonObject.string(key: String): String? = (get(key) as? JsonPrimitive)?.takeIf { it.isString }?.content


/** Own work stopped on a failure before the node's result: the node did not respond, no server, or the upload failed. */
private fun paused(process: ProcessState?): ProcessState? =
    process?.takeIf { (it.stage == ProcessStage.FAILED || it.stage == ProcessStage.AUDIO_ONLY) && it.problem != null }

// remarks are the server's text cached in history; [problem] — own work failing before the result
private fun recordingRemarks(meta: JsonObject?, problem: String?): List<String> {
    val remarks = (Meta.lastRun(meta)?.get("remarks") as? JsonArray).orEmpty()
        .mapNotNull { (it as? JsonPrimitive)?.takeIf { it.isString }?.content?.takeIf(String::isNotBlank) }
    return (listOfNotNull(problem?.takeIf { it.isNotBlank() }) + remarks).distinct()
}

// code of the remarks.track_silent remark with a reason, checked by fixture silent-reason-v1
private const val OTHER_SIDE_SILENT = "other_side_silent"

/** The short text of the remark that the other side's voice was not recorded, while the last run in history ([Meta.lastRun]) has it. */
private fun otherSideWarning(meta: JsonObject?): String? =
    (Meta.lastRun(meta)?.get("notes") as? JsonArray).orEmpty().firstNotNullOfOrNull { note ->
        (note as? JsonObject)?.takeIf { it.string("code") == OTHER_SIDE_SILENT }?.string("short")?.takeIf(String::isNotBlank)
    }

private fun retrySummary(dir: File, paused: ProcessState?): Boolean {
    if (paused?.stage == ProcessStage.FAILED) return paused.summaryOnly
    val run = Meta.lastRun(dir) ?: return false
    if (run.string("kind") == "summary" && run.string("status") == "failed") return true
    return (run["notes"] as? JsonArray).orEmpty().any {
        val note = it as? JsonObject
        note?.string("step") in setOf("summary", "title") && note?.string("code") != TITLE_NOT_APPLIED
    }
}

private fun read(file: File): String? = try {
    file.readText(Charsets.UTF_8)
} catch (_: IOException) {
    null
}
