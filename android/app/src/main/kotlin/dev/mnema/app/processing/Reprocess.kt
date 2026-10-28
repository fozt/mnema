package dev.mnema.app.processing

import dev.mnema.app.i18n.tr
import dev.mnema.app.model.Lang
import dev.mnema.app.recordings.freeName
import dev.mnema.app.recordings.outcomeKey
import dev.mnema.app.recordings.partMark
import dev.mnema.app.recordings.partOf
import dev.mnema.app.recordings.recordingId
import dev.mnema.app.recordings.startedIso
import dev.mnema.app.transcript.Meta
import dev.mnema.app.transcript.Wav
import java.io.File
import java.io.IOException
import java.util.Locale
import java.util.UUID
import kotlinx.serialization.Serializable

/**
 * The choice in the "Process again" window applies to this processing only; null — as in settings; [autoName] — name any folder by its
 * content. [title] — own title from the window: in the work file it is [ProcessState.userTitle].
 */
@Serializable
data class ReprocessOptions(
    val lang: Lang? = null,
    val asr: String? = null,
    val title: String? = null,
    val startS: Double? = null,
    val endS: Double? = null,
    val autoName: Boolean = false,
    val tracks: Set<String>? = null,
)

private const val PCM_BYTES_PER_SECOND = 16_000 * 2
private const val MIB = 1L shl 20
private const val SPARE_BYTES = 16 * MIB

/** The whole recording is selected: the fields show whole seconds, 1:35:00 of 1:35:00.8 (fixture offsets-v1, like whole on desktop). */
fun isWhole(startS: Double, endS: Double, length: Double): Boolean = startS < 1 && endS >= length - 1

/** Like fmt_clock on desktop: always "h:mm:ss". */
internal fun fmtClock(seconds: Double): String {
    val total = seconds.toLong()
    return String.format(Locale.ROOT, "%d:%02d:%02d", total / 3600, total % 3600 / 60, total % 60)
}

private fun tracks(dir: File): List<File> =
    dir.listFiles { file -> file.isFile && file.name.endsWith(".wav") && file.length() > Wav.HEADER }
        ?.sortedBy { it.name }.orEmpty()

private fun lengthOf(wavs: List<File>): Double = wavs.maxOfOrNull(Wav::seconds) ?: 0.0

/** Part [startS, endS) into a sibling folder "<name> (<mark>)" with trimmed tracks, like prepare_recovery on desktop; the source stays intact. */
@Suppress("UsableSpace") // a rough space check, like disk_usage on desktop: a shared folder, not the app cache
fun sliceRecording(dir: File, startS: Double, endS: Double, id: String = UUID.randomUUID().toString()): File {
    val wavs = tracks(dir)
    require(wavs.isNotEmpty()) { tr("{name} has no recorded audio", "name" to dir.name) }
    wavs.forEach(Wav::repair)
    val length = lengthOf(wavs)
    val a = startS.coerceAtLeast(0.0)
    val b = minOf(endS, length)
    require(a < length) { tr("Start {start} is after the end of the recording ({end})", "start" to fmtClock(a), "end" to fmtClock(length)) }
    require(b > a) { tr("The end of the range is before its start") }
    val parent = requireNotNull(dir.absoluteFile.parentFile) { tr("{name} has no parent folder", "name" to dir.name) }
    val need = wavs.sumOf { ((b - a) * PCM_BYTES_PER_SECOND).toLong() } + SPARE_BYTES
    if (parent.usableSpace < need) throw IOException(tr("Not enough disk space: the part of the recording needs ~{size} MB", "size" to need / MIB))
    val part = freeName(File(parent, "${dir.name} (${partOf(a, b)})"))
    if (!part.mkdir()) throw IOException(tr("Couldn't create the folder {name}", "name" to part.name))
    var cut = false
    try {
        wavs.forEach { Wav.slice(it, File(part, it.name), a, b) }
        Meta.createPart(dir, part, id)
        cut = true
    } finally {
        // no half-written part is left
        if (!cut) part.deleteRecursively()
    }
    return part
}

/** The "Process again" state is written; a part goes to a new sibling folder. Returns the processing folder, null — refused. */
fun reprocessed(dir: File, root: File, options: ReprocessOptions, newId: () -> String): File? {
    if (!WorkScheduler.accepts(dir, root, summaryOnly = false)) return null
    return try {
        val wavs = tracks(dir)
        wavs.forEach(Wav::repair)
        val length = lengthOf(wavs)
        val a = (options.startS ?: 0.0).coerceAtLeast(0.0)
        val b = minOf(options.endS ?: length, length)
        if (isWhole(a, b, length)) {
            Meta.ensureRecording(dir, newId()) { startedIso(dir) }
            val id = recordingId(dir) ?: throw IOException(tr("The recording has no id"))
            val previous = ProcessStateFile.read(dir)
            // the folder is itself a part of a recording: its place in the source and the "(0.10–1.35)" mark stay
            val kept = previous?.takeIf { it.id == id }?.options
            val state = queued(previous, summaryOnly = false, options.copy(startS = kept?.startS, endS = kept?.endS), id, outcomeKey(Meta.read(dir)))
            ProcessStateFile.write(dir, state)
            return dir
        }
        val id = newId()
        val part = sliceRecording(dir, a, b, id)
        try {
            val since = outcomeKey(Meta.read(part))
            ProcessStateFile.write(part, withOwnTitle(ProcessState(id = id, options = options.copy(startS = a, endS = b), since = since), partMark(part.name)))
        } catch (e: IOException) {
            part.deleteRecursively()
            throw e
        }
        part
    } catch (_: IOException) {
        null
    } catch (_: IllegalArgumentException) {
        null
    }
}
