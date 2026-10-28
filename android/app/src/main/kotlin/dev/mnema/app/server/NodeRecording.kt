package dev.mnema.app.server

import dev.mnema.app.i18n.tr
import dev.mnema.app.model.AUTO_ASR
import dev.mnema.app.recordings.NAME_MAX_BYTES
import dev.mnema.app.transcript.RecordingFolder
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

@Serializable
data class Part(val start: Double, val end: Double)

@Serializable
data class RecordingParams(
    val started: String,
    val name: String,
    val user_title: String? = null,
    val lang: String? = null,
    val asr: String = AUTO_ASR,
    val summary: Boolean = true,
    val summary_lang: String? = null,
    val part: Part? = null,
    // why the other side had no audio at all: blocked — a call was going, none — there was no call; refines the server's remark
    val silent_reason: Map<String, String> = emptyMap(),
    val recording_error: String? = null,
    val retitle: Boolean? = null,
    // a track failed: true — text of the other tracks, false — a failure without files, the previous transcript stays
    val partial: Boolean = false,
)

@Serializable
data class SummaryParams(
    val started: String,
    val name: String,
    val summary_lang: String? = null,
    val past: JsonObject? = null,
)

/** Result of a recording job, like `ops.RecordingResult`: each file is text or null (delete), plus the folder name and a history run. */
@Serializable
data class RecordingResult(
    val files: Map<String, String?> = emptyMap(),
    val folder: String? = null,
    val run: JsonObject,
) {
    /** An unknown file, or a transcript in a summary result, is refused before writing; an invalid folder name means no rename. */
    fun checked(summaryOnly: Boolean): RecordingResult {
        if (files.keys.any { it !in RESULT_FILES }) throw RefusedResult(tr("The server returned an unknown recording file"))
        if (summaryOnly && RecordingFolder.TRANSCRIPT in files) throw RefusedResult(tr("A summary must not change the transcript"))
        return copy(folder = folder?.takeIf(::validFolderName))
    }
}

/** The node's result cannot be applied, and the same reply will not fit on the next poll either. */
class RefusedResult(message: String) : Exception(message)

private val RESULT_FILES = setOf(RecordingFolder.TRANSCRIPT, RecordingFolder.SUMMARY, "translation.md")

/** A folder name from the result: no path, no control characters, at most NAME_MAX_BYTES. */
internal fun validFolderName(name: String): Boolean =
    name.isNotBlank() && !name.startsWith('.') &&
        name.none { it == '/' || it == '\\' || it.isISOControl() || it == '\u2028' || it == '\u2029' } &&
        name.codePoints().noneMatch { it in 0xD800..0xDFFF } && name.toByteArray(Charsets.UTF_8).size <= NAME_MAX_BYTES

/** Why a job ended with an error, like `jobs.JobErrorKind`. */
object JobErrorKind {
    const val CANCELLED = "cancelled"
    const val RESTARTED = "restarted"
}

@Serializable
data class NodeJob(
    val id: String,
    val status: String,
    val progress: Double? = null,
    val message: String = "",
    val result: RecordingResult? = null,
    val error: String? = null,
    val error_kind: String? = null,
)

@Serializable
data class AsrOption(val id: String, val title: String)

@Serializable
internal data class AsrChoices(val choices: List<AsrOption>)
