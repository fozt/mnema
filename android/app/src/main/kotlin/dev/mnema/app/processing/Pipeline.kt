package dev.mnema.app.processing

import dev.mnema.app.Redact
import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.helper.helperText
import dev.mnema.app.i18n.tr
import dev.mnema.app.recordings.moveFolder
import dev.mnema.app.recordings.ProcessingState
import dev.mnema.app.recordings.renameRecording
import dev.mnema.app.recordings.chosenTitle
import dev.mnema.app.recordings.outcomeKey
import dev.mnema.app.recordings.partMark
import dev.mnema.app.recordings.recordingId
import dev.mnema.app.recordings.startedIso
import dev.mnema.app.server.JobErrorKind
import dev.mnema.app.server.Part
import dev.mnema.app.server.RecordingParams
import dev.mnema.app.server.RecordingResult
import dev.mnema.app.server.RefusedResult
import dev.mnema.app.server.RunnerClient
import dev.mnema.app.server.RunnerErrorKind
import dev.mnema.app.server.RunnerException
import dev.mnema.app.server.SummaryParams
import dev.mnema.app.server.UploadInfo
import dev.mnema.app.settings.Settings
import dev.mnema.app.sync.filesLock
import dev.mnema.app.transcript.Meta
import dev.mnema.app.transcript.Wav
import java.io.File
import java.io.FileNotFoundException
import java.io.IOException
import java.io.RandomAccessFile
import java.net.URI
import java.net.URISyntaxException
import java.util.Locale
import java.util.TimeZone
import java.util.UUID
import kotlin.coroutines.cancellation.CancellationException
import kotlin.math.roundToLong
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.runInterruptible
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.longOrNull

sealed interface StepResult {
    data object Done : StepResult
    data class Again(val delayMs: Long) : StepResult
    data object AudioOnly : StepResult
    data class Failed(val message: String) : StepResult
}

/** One short step; uploads and the shared job key survive the process stopping. */
class Pipeline(
    private val settings: Settings,
    private val runner: RunnerClient?,
    private val encoder: AudioEncoder,
    private val cacheDir: File,
    private val clock: () -> Long,
    private val recordingNow: () -> String?,
    private val log: (String) -> Unit = {},
    private val wait: suspend (Long) -> Unit = { delay(it) },
    // the recording folder by id after a rename: the copy rule that accounts for sync (WorkScheduler.findFolder)
    private val findFolder: (File, String) -> File? = { root, id -> WorkScheduler.findFolder(root, id) },
    // moves the recording folder under the result name; if Android refuses, the result stays in the old folder (applyResult)
    private val moveFolder: (File, File) -> Unit = ::moveFolder,
) {
    suspend fun step(dir: File, deadlineMs: Long, onProgress: (ProcessingState) -> Unit): Pair<StepResult, File> {
        if (dir.name == recordingNow()) return StepResult.Again(BUSY_MS) to dir
        val state = try {
            Meta.ensureRecording(dir, UUID.randomUUID().toString()) { startedIso(dir) }
            val id = recordingId(dir) ?: throw IOException(tr("The recording has no id"))
            ProcessStateFile.own(dir) ?: ProcessState(id = id, since = outcomeKey(Meta.read(dir)))
        } catch (e: IOException) {
            log("Result not saved: ${Redact.redact(e.message.orEmpty())}")
            return notStarted(dir, Problems.WRITE) to dir
        }
        when (state.stage) {
            ProcessStage.DONE -> return StepResult.Done to dir
            ProcessStage.FAILED -> return StepResult.Failed(state.problem.orEmpty()) to dir
            ProcessStage.STOPPED, ProcessStage.AUDIO_ONLY -> return StepResult.AudioOnly to dir
        }
        val work = Work(dir, state, onProgress)
        val result = try {
            work.process(deadlineMs)
        } catch (e: CancellationException) {
            throw e
        } catch (e: RunnerException) {
            when (e.kind) {
                RunnerErrorKind.UNREACHABLE -> work.failed(Problems.nodeDown(nodeName()), unreachable = true)
                RunnerErrorKind.TOKEN -> work.failed(Problems.TOKEN)
                else -> work.failed(e.message.orEmpty())
            }
        } catch (e: IOException) {
            work.writeFailed(e)
        } catch (e: Exception) {
            log("Processing failure: ${e.javaClass.simpleName}: ${Redact.redact(e.message.orEmpty())}")
            work.failed(Problems.PHONE_FAILURE)
        }
        return result to work.dir
    }

    /**
     * The recording history can't be written (newer than the app, a symlink, no space): the failure goes to the recording's own
     * work, the card shows it, and the work is not queued again on every start; without a recording id — retry later, as on a file write failure.
     */
    private fun notStarted(dir: File, problem: String): StepResult = synchronized(filesLock) {
        val id = recordingId(dir) ?: return@synchronized StepResult.Again(POLL_AGAIN_MS)
        val own = ProcessStateFile.own(dir) ?: ProcessState(id = id)
        try {
            ProcessStateFile.write(dir, own.copy(stage = ProcessStage.FAILED, problem = problem, since = outcomeKey(Meta.read(dir))))
        } catch (_: IOException) {
            return@synchronized StepResult.Again(POLL_AGAIN_MS)
        }
        StepResult.Failed(problem)
    }

    // the name the node gave itself before a new job of this processing, otherwise the one from the node list
    private var checkedName: String? = null

    private fun nodeName(): String {
        val url = runner?.connection?.url.orEmpty()
        return checkedName ?: settings.nodes.firstOrNull { it.address == url }?.name ?: host(url) ?: url
    }

    private inner class Work(var dir: File, var state: ProcessState, val onProgress: (ProcessingState) -> Unit) {
        private var uploadsLeft = UPLOADS_PER_STEP
        private var encodeMsPerByte: Double? = null
        private val unencodable = mutableSetOf<String>()
        // without a choice in the window, a source muted for the whole recording is not recognized, like an unselected one; the server decides on silence
        private val muted by lazy { mutedTracks(dir) }
        private val selected get() = TRACKS.filter { label -> state.options?.tracks?.let { label in it } ?: (label !in muted) }

        suspend fun process(deadlineMs: Long): StepResult {
            val runner = runner ?: return audioOnly(Problems.NO_NODE)
            if (state.node != runner.connection.url) {
                save(state.copy(node = runner.connection.url, tracks = emptyMap(), key = null, job = null,
                    sentName = null, stage = ProcessStage.NEW, retries = 0, resends = 0))
            }
            // history is written last: the result is already applied even if DELETE did not make it.
            state.job?.let { id ->
                if (runFor(id) != null) return finish(runner, id) // the result is already applied
            }
            if (state.job == null) {
                if (state.stage == ProcessStage.NEW || state.tracks.isEmpty()) prepare()?.let { return it }
                compressAll(deadlineMs)?.let { return it }
                for ((label, track) in state.tracks) {
                    if (track.upload == null) {
                        try { send(runner, label, deadlineMs)?.let { return it } } catch (e: RunnerException) {
                            if (e.kind == RunnerErrorKind.UNREACHABLE || e.kind == RunnerErrorKind.TOKEN) throw e
                            return failed(Problems.upload(e.message.orEmpty()))
                        }
                    }
                }
                if (state.key == null) {
                    // a new job asks what the node is called now: failures name it by its new name
                    runner.capabilities().name?.let { checkedName = it }
                    save(state.copy(key = "${state.id}-${UUID.randomUUID()}", sentName = dir.name))
                }
                val job = try {
                    // one technical retry only when the body was sent but no answer came.
                    try { submit(runner) } catch (e: RunnerException) {
                        if (!e.requestSent) throw e
                        submit(runner)
                    }
                } catch (e: RunnerException) {
                    if (e.kind == RunnerErrorKind.KEY_TAKEN) return renewed(e.message.orEmpty())
                    if (e.kind != RunnerErrorKind.UPLOAD_GONE) throw e
                    // the track is not from this processing: don't send the sound in circles
                    if (e.track != null && e.track !in state.tracks) return failed(Problems.upload(e.message.orEmpty()))
                    val labels = e.track?.let(::setOf) ?: state.tracks.keys
                    // as on desktop: at most as many re-submissions as there are tracks
                    val spent = state.resends >= state.tracks.size
                    save(state.copy(resends = state.resends + 1, tracks = state.tracks.mapValues { (label, track) ->
                        if (label in labels) TrackState(key = track.key) else track
                    }))
                    return if (spent) failed(Problems.upload(e.message.orEmpty())) else StepResult.Again(SOON_MS)
                }
                save(state.copy(job = job, stage = ProcessStage.ASR, problem = null, resends = 0))
            }
            val id = requireNotNull(state.job)
            var job = runner.nodeJob(id) ?: return renewed(Problems.JOB_LOST)
            // the node's share holds for the whole step: between steps the card would only know "recognizing"
            while (job.status == "pending" || job.status == "running") {
                // waiting in the server queue — "Queued", not "Recognizing"
                onProgress(if (job.status == "pending") ProcessingState.Queued else ProcessingState.Recognizing(job.progress, job.message))
                if (clock() + POLL_MS > deadlineMs) return StepResult.Again(POLL_MS)
                wait(POLL_MS)
                job = runner.nodeJob(id) ?: return renewed(Problems.JOB_LOST)
            }
            return when (job.status) {
                "done" -> {
                    val result = job.result ?: return failed(tr("The server returned no processing result"))
                    try {
                        // own title from the card: the folder name the server made up is not taken; one removed after sending neither
                        val applying = if (ownTitle() != null || state.sentTitle != null) result.copy(folder = null) else result
                        dir = applyResult(dir, applying, id, host(runner.connection.url), state.sentName ?: dir.name, recordingNow(), state.summaryOnly,
                            moveFolder, log)
                        // this work wrote the outcome into the history: its mark is the new one
                        save(state.copy(problem = null, since = outcomeKey(Meta.read(dir))))
                    } catch (e: RefusedResult) {
                        // as on desktop: a failure, the job stays — an updated app will apply its result
                        return failed(e.message.orEmpty())
                    } catch (e: IOException) {
                        // the rename may have happened before the history was written; the state travels with the folder.
                        if (!dir.exists()) dir.parentFile?.let { root -> findFolder(root, state.id) }?.let { dir = it }
                        log("Result not saved: ${Redact.redact(e.message.orEmpty())}")
                        save(state.copy(problem = Problems.WRITE))
                        return StepResult.Again(POLL_AGAIN_MS)
                    }
                    finish(runner, id)
                }
                "error" -> when (job.error_kind) {
                    JobErrorKind.CANCELLED -> { save(stopped(state)); StepResult.AudioOnly }
                    JobErrorKind.RESTARTED -> renewed(job.error ?: Problems.JOB_LOST)
                    else -> failed(job.error ?: tr("The server didn't process the recording"), clearJob = true)
                }
                else -> failed(tr("The server answered unexpectedly"))
            }
        }

        // the node has no job for the phone (it restarted, forgot it, the key is taken by someone else): a new one with a new key
        // and the same uploads, once — the node names a missing upload when the job is submitted
        private suspend fun renewed(reason: String): StepResult {
            if (state.retries >= RENEWALS) return failed(reason, clearJob = true)
            save(state.copy(key = null, job = null, sentName = null, retries = state.retries + 1))
            return StepResult.Again(SOON_MS)
        }

        private suspend fun prepare(): StepResult? {
            onProgress(ProcessingState.Preparing)
            val tracks = if (state.summaryOnly) {
                val file = File(dir, "transcript.txt")
                if (!file.isFile || file.readText(Charsets.UTF_8).isBlank()) return failed(Problems.NO_TRANSCRIPT)
                listOf("transcript")
            } else {
                selected.map(::wav).filter { it.isFile }.forEach(Wav::repair)
                selected.filter { wav(it).isFile && wav(it).length() > Wav.HEADER }
            }
            if (tracks.isEmpty()) return audioOnly(Problems.NO_SOUND)
            save(state.copy(tracks = tracks.associateWith { label ->
                val file = wav(label)
                val key = "${file.length()}:${file.lastModified()}"
                state.tracks[label]?.takeIf { it.key == key } ?: TrackState(key = key)
            }, stage = if (state.summaryOnly) ProcessStage.SUMMARY else ProcessStage.ASR,
                started = state.started.ifEmpty { Meta.isoNow(clock(), TimeZone.getDefault()) }, problem = null))
            return null
        }

        private suspend fun submit(runner: RunnerClient): String {
            // the start is unknown — the node needs some value, it is not written to meta
            val started = Meta.started(dir) ?: Meta.isoNow(clock(), TimeZone.getDefault())
            val name = requireNotNull(state.sentName)
            val title = ownTitle().takeUnless { state.summaryOnly }
            save(state.copy(sentTitle = title))
            if (state.summaryOnly) return runner.submitRecordingSummary(requireNotNull(state.key),
                requireNotNull(track("transcript").upload), SummaryParams(started, name, settings.summaryLang.code, Meta.lastRun(dir)))
            val options = state.options
            val part = options?.startS?.let { start -> options.endS?.let { end -> Part(start, end) } }
            return runner.submitRecording(requireNotNull(state.key), state.tracks.mapValues { requireNotNull(it.value.upload) },
                RecordingParams(started, name, title,
                    (options?.lang ?: settings.lang).code, options?.asr ?: settings.asr,
                    summary_lang = settings.summaryLang.code, part = part,
                    silent_reason = otherSideSilence(dir)?.takeIf { "system" in selected }?.let { mapOf("system" to it) }.orEmpty(),
                    recording_error = recordingError(dir),
                    retitle = true.takeIf { options?.autoName == true }, partial = !hasText(File(dir, "transcript.txt"))))
        }

        // as on desktop: a new recording keeps the recognized half, reprocessing does not lose the previous text
        private fun hasText(file: File): Boolean = file.isFile && file.readText(Charsets.UTF_8).isNotBlank()

        private fun runFor(id: String): JsonObject? =
            (Meta.read(dir)?.get("runs") as? JsonArray).orEmpty().mapNotNull { it as? JsonObject }
                .firstOrNull { (it["job"] as? JsonPrimitive)?.content == id }

        /** The node outcome is already in the history: the work marks it as its own ([ProcessState.since]); a failure is an outcome too, without its own problem. */
        private suspend fun finish(runner: RunnerClient, id: String): StepResult {
            val run = runFor(id) ?: throw IOException(tr("The result history can't be read"))
            runner.forget(id)
            val since = outcomeKey(Meta.read(dir))
            if ((run["status"] as? JsonPrimitive)?.content == "failed") {
                val reason = (run["error"] as? JsonPrimitive)?.takeIf { it.isString }?.content?.takeIf { it.isNotBlank() }
                    ?: tr("The server didn't process the recording")
                save(state.copy(stage = ProcessStage.FAILED, problem = null, unreachable = false, job = null, key = null,
                    sentName = null, since = since))
                return StepResult.Failed(Redact.redact(reason))
            }
            currentCoroutineContext().ensureActive()
            // the title and the end of processing under one lock: one set at the last moment won't be left without a folder
            synchronized(filesLock) {
                applyOwnTitle()
                commit(state.copy(stage = ProcessStage.DONE, problem = null, since = since))
            }
            cacheDir.listFiles { file -> file.name.startsWith("${state.id}-") }?.forEach { it.delete() }
            return StepResult.Done
        }

        suspend fun writeFailed(error: IOException): StepResult {
            if (!dir.exists()) dir.parentFile?.let { root -> findFolder(root, state.id) }?.let { dir = it }
            log("Result not saved: ${Redact.redact(error.message.orEmpty())}")
            val next = state.copy(problem = Problems.WRITE)
            try { save(next) } catch (_: IOException) { /* space or access not restored yet */ }
            return StepResult.Again(POLL_AGAIN_MS)
        }

        suspend fun failed(message: String, clearJob: Boolean = false, unreachable: Boolean = false): StepResult {
            val problem = Redact.redact(message)
            save(state.copy(stage = ProcessStage.FAILED, problem = problem, unreachable = unreachable,
                job = state.job.takeUnless { clearJob }, key = state.key.takeUnless { clearJob },
                sentName = state.sentName.takeUnless { clearJob }))
            return StepResult.Failed(problem)
        }

        private suspend fun audioOnly(problem: String): StepResult {
            save(state.copy(stage = ProcessStage.AUDIO_ONLY, problem = problem))
            return StepResult.AudioOnly
        }

        /** Tracks still to go to the server are compressed before sending: the sent share is by bytes of all of them. */
        private suspend fun compressAll(deadlineMs: Long): StepResult? {
            for ((label, track) in state.tracks) {
                if (track.state == TrackState.NEW && prepared(label, deadlineMs) == null) return StepResult.Again(SOON_MS)
            }
            return null
        }

        /** What to send: the compressed track, or WAV if the codec failed; null — compression can't finish by the end of the step. */
        private suspend fun prepared(label: String, deadlineMs: Long): File? {
            val wav = wav(label)
            if (state.summaryOnly) return wav
            val compressed = compressed(label)
            if (compressed.isFile) return compressed
            if (!wav.isFile || label in unencodable) return wav
            // work done is not lost: the compressed track stays in the cache, the next step compresses the rest
            val needed = encodeMsPerByte?.let { (it * wav.length()).roundToLong() }
            if (needed != null && clock() + needed > deadlineMs) return null
            val begun = clock()
            val done = compress(wav, compressed)
            encodeMsPerByte = (clock() - begun).toDouble() / maxOf(1L, wav.length())
            save(state.copy(steps = added(prepareStep(label), begun)))
            val length = clockText(Wav.seconds(wav))
            log("Audio prepared $label: $length in ${clockText(state.steps.getValue(prepareStep(label)))}")
            if (done) return compressed
            // the codec failed — send WAV: slower, but the recording is not lost
            unencodable += label
            return wav
        }

        private suspend fun compress(wav: File, out: File): Boolean {
            onProgress(ProcessingState.Preparing)
            return try {
                cacheDir.mkdirs()
                runInterruptible { encoder.encode(wav, out) }
                out.isFile
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                false
            }
        }

        /** null — the track is on the server (UPLOADED); otherwise the step outcome, the upload offset saved. */
        private suspend fun send(runner: RunnerClient, label: String, deadlineMs: Long): StepResult? {
            val file = prepared(label, deadlineMs) ?: return StepResult.Again(SOON_MS)
            if (!file.isFile) throw FileNotFoundException(file.name)
            val name = "$label.${file.extension}"
            val size = file.length()
            var mark = clock()
            onProgress(ProcessingState.Uploading(sentShare()))
            // the node has the truth: continue from what it accepted
            var id = track(label).uploadId
            var offset = 0L
            if (id != null) {
                val info = runner.uploadOffset(id)
                when {
                    info == null || !info.fits(name, size) -> id = null
                    info.path != null -> return uploaded(label, info.path, mark, size)
                    // the whole file is on the node, but the last chunk is still being written
                    info.offset == size -> return StepResult.Again(POLL_AGAIN_MS)
                    else -> offset = info.offset
                }
            }
            val buffer = ByteArray(minOf(CHUNK_BYTES.toLong(), maxOf(1L, size)).toInt())
            var chunks = 0
            var chunkMs = 0L
            val raf = RandomAccessFile(file, "r")
            try {
                while (true) {
                    if (id == null) {
                        id = runner.startUpload(name, size)
                        offset = 0
                        mark = sentUpTo(label, id, offset, mark)
                    }
                    if (chunks > 0 && clock() + chunkMs > deadlineMs) return StepResult.Again(SOON_MS)
                    val count = minOf(buffer.size.toLong(), size - offset).toInt()
                    raf.seek(offset)
                    raf.readFully(buffer, 0, count)
                    val begun = clock()
                    var info = try {
                        runner.uploadChunk(id, offset, buffer, count)
                    } catch (e: RunnerException) {
                        if (e.kind != RunnerErrorKind.UPLOAD_GONE) throw e
                        null
                    }
                    chunkMs = clock() - begun
                    chunks++
                    // the whole file is on the node but there is no path: the answer to the last chunk was lost, ask for the path separately
                    if (info != null && info.path == null && info.offset == size) {
                        info = runner.uploadOffset(id)?.takeIf { it.fits(name, size) }
                    }
                    info?.path?.let { return uploaded(label, it, mark, size) }
                    // the upload expired or the node answered nonsense — start over
                    if (info == null || info.offset !in 0..size) {
                        if (uploadsLeft-- <= 0) return StepResult.Again(POLL_AGAIN_MS)
                        id = null
                        continue
                    }
                    val moved = info.offset != offset
                    offset = info.offset
                    mark = sentUpTo(label, id, offset, mark)
                    // the chunk was not accepted or the whole file has no path: the node still holds the previous upload
                    if (!moved || offset == size) return StepResult.Again(POLL_AGAIN_MS)
                }
            } finally {
                raf.close()
            }
        }

        private suspend fun sentUpTo(label: String, id: String, offset: Long, mark: Long): Long {
            val track = track(label).copy(uploadId = id, uploaded = offset)
            save(state.copy(tracks = state.tracks + (label to track), steps = added(uploadStep(label), mark)))
            onProgress(ProcessingState.Uploading(sentShare()))
            return clock()
        }

        private suspend fun uploaded(label: String, path: String, mark: Long, size: Long): StepResult? {
            val track = track(label).copy(upload = path, state = TrackState.UPLOADED, uploadId = null, uploaded = 0)
            save(state.copy(tracks = state.tracks + (label to track), steps = added(uploadStep(label), mark)))
            onProgress(ProcessingState.Uploading(sentShare()))
            val megabytes = String.format(Locale.ROOT, "%.1f", size / 1_000_000.0)
            log("Upload $label: $megabytes MB in ${clockText(state.steps.getValue(uploadStep(label)))}")
            return null
        }

        // sent share by bytes of all tracks of this processing
        private fun sentShare(): Double? {
            var total = 0L
            var sent = 0L
            for ((label, track) in state.tracks) {
                val compressed = compressed(label)
                val file = when {
                    compressed.isFile -> compressed
                    track.state == TrackState.NEW || track.upload != null -> wav(label)
                    else -> continue
                }
                val size = file.length()
                total += size
                sent += if (track.state == TrackState.NEW) track.uploaded.coerceIn(0, size) else size
            }
            return if (total > 0) sent.toDouble() / total else null
        }

        // step time accumulates in the state: a long recording is prepared and sent in several steps
        private fun added(step: String, fromMs: Long): Map<String, Double> =
            state.steps + (step to (state.steps[step] ?: 0.0) + (clock() - fromMs) / 1000.0)

        private fun track(label: String): TrackState = state.tracks.getValue(label)

        private suspend fun save(next: ProcessState) {
            currentCoroutineContext().ensureActive()
            commit(next)
        }

        // own title is set in the card, also during a step: the one on disk is newer
        private fun commit(next: ProcessState) = synchronized(filesLock) {
            val saved = ProcessStateFile.read(dir)?.takeIf { it.id == next.id }
            val merged = if (saved != null) next.copy(userTitle = saved.userTitle) else next
            ProcessStateFile.write(dir, merged)
            state = merged
        }

        private fun ownTitle(): String? {
            val saved = ProcessStateFile.read(dir)?.takeIf { it.id == state.id }
            return chosenTitle(if (saved != null) saved.userTitle else state.userTitle, partMark(dir.name))
        }

        // the folder follows the manual title rules, like "Rename"; if that fails the old name stays and the title is dropped
        private fun applyOwnTitle() {
            val own = ownTitle() ?: return
            if (dir.name == recordingNow()) return
            try {
                dir = renameRecording(dir, own)
                state = ProcessStateFile.read(dir)?.takeIf { it.id == state.id } ?: state.copy(userTitle = null)
            } catch (e: IOException) {
                log("Own recording title not applied: ${Redact.redact(e.message.orEmpty())}")
                state = state.copy(userTitle = null)
                ProcessStateFile.write(dir, state)
            }
        }

        private fun wav(label: String) = File(dir, if (label == "transcript") "transcript.txt" else "$label.wav")

        private fun compressed(label: String): File {
            val wav = wav(label)
            return File(cacheDir, "${state.id}-$label-${wav.length()}-${wav.lastModified()}.m4a")
        }
    }

    private companion object {
        val TRACKS = listOf("mic", "system")
        const val BUSY_MS = 30_000L
        const val POLL_MS = 5_000L
        const val POLL_AGAIN_MS = 30_000L
        const val SOON_MS = 1_000L
        const val UPLOADS_PER_STEP = 6
        const val RENEWALS = 1
        const val CHUNK_BYTES = 4 * 1024 * 1024
        fun prepareStep(label: String) = "prepare_$label"
        fun uploadStep(label: String) = "upload_$label"
        fun clockText(seconds: Double): String {
            val total = seconds.roundToLong()
            val (h, m, sec) = Triple(total / 3600, total % 3600 / 60, total % 60)
            return if (h > 0) String.format(Locale.ROOT, "%d:%02d:%02d", h, m, sec) else String.format(Locale.ROOT, "%d:%02d", m, sec)
        }
        fun UploadInfo.fits(name: String, size: Long): Boolean =
            (this.name == null || this.name == name) && (this.size == null || this.size == size) && (path != null || offset in 0..size)
        fun host(url: String): String? = try {
            val uri = URI(url)
            uri.host?.removeSurrounding("[", "]")?.let { if (uri.port >= 0) "$it:${uri.port}" else it }
        } catch (_: URISyntaxException) { null }
    }
}

/** Tracks muted on the record screen all the time, by the helper summary (Capture.Stats): every recorded sample is silence. */
internal fun mutedTracks(dir: File): Set<String> {
    val report = try { Json.parseToJsonElement(File(dir, ".mnema-helper-report.json").readText()) as? JsonObject }
        catch (_: IOException) { null } catch (_: IllegalArgumentException) { null } ?: return emptySet()
    return mapOf("mic" to "mic_total", "system" to "total").filterValues { key ->
        val total = report[key] as? JsonObject
        val samples = (total?.get("samples") as? JsonPrimitive)?.longOrNull ?: 0
        val muted = (total?.get("muted_samples") as? JsonPrimitive)?.longOrNull ?: 0
        samples > 0 && muted >= samples
    }.keys
}

/** A recording cut off per the helper report (Capture.writeReport): status error, the reason (English from the helper) — error_reason. */
internal fun recordingError(dir: File): String? {
    val report = try { Json.parseToJsonElement(File(dir, ".mnema-helper-report.json").readText()) as? JsonObject }
        catch (_: IOException) { null } catch (_: IllegalArgumentException) { null }
    if (report == null || (report["status"] as? JsonPrimitive)?.content != "error") return null
    val reason = (report["error_reason"] as? JsonPrimitive)?.takeIf { it.isString }?.content
    return reason?.takeIf { it.isNotBlank() }?.let(::helperText)?.take(500)
}

/**
 * Why the other side had no sound for a single second, per the helper report (Capture.addSeconds): blocked — a call went on but
 * no sound came, none — there was no call; null — there was sound or the report says nothing about seconds.
 */
internal fun otherSideSilence(dir: File): String? {
    val report = try { Json.parseToJsonElement(File(dir, HelperProtocol.REPORT).readText()) as? JsonObject }
        catch (_: IOException) { null } catch (_: IllegalArgumentException) { null }
    val seconds = report?.get("seconds") as? JsonObject ?: return null
    fun count(key: String): Long? = (seconds[key] as? JsonPrimitive)?.longOrNull
    val total = count("total") ?: return null
    if (total <= 0 || count("call_sound") != 0L) return null
    return if ((count("cellular") ?: 0) + (count("messenger") ?: 0) > 0) "blocked" else "none"
}
