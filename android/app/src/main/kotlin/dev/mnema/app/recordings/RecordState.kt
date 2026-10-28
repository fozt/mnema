package dev.mnema.app.recordings

import dev.mnema.app.transcript.Wav
import java.io.File
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

// Port of mnema/live/meta.record_state: the same recording-state rule as the desktop, shared fixture record-state-v1.

/** Recording state: the outcome comes from the `.mnema-meta.json` history; the work file does not store it. */
enum class RecordState { UNPROCESSED, PROCESSING, PROCESSED, NO_SPEECH, AUDIO_ONLY, FAILED }

/** The phone's work file: work is running, was interrupted, or there is none. */
enum class Work { NONE, RUNNING, INTERRUPTED }

private val TEXTS = setOf("transcript.txt", "summary.md")
private val OUTCOMES = setOf("ok", "failed", "stopped")

private fun JsonObject.text(key: String): String? = (get(key) as? JsonPrimitive)?.takeIf { it.isString }?.content

private fun runs(meta: JsonObject?): List<JsonObject> = (meta?.get("runs") as? JsonArray).orEmpty().mapNotNull { it as? JsonObject }

private fun outcomes(meta: JsonObject?): List<JsonObject> =
    runs(meta).filter { (it.text("kind") ?: "process") == "process" && it.text("status") in OUTCOMES }

/** Outcome from history: the last processing run (not a summary run) with status ok or failed; a stopped one counts only when no outcome precedes it. */
fun outcome(meta: JsonObject?): JsonObject? {
    val finished = outcomes(meta)
    return finished.lastOrNull { it.text("status") != "stopped" } ?: finished.lastOrNull()
}

/** The last processing run, stopped ones included, finished: audio cleanup keeps the audio while a newer run was stopped. */
fun processed(meta: JsonObject?): Boolean = outcomes(meta).lastOrNull()?.text("status") == "ok"

/** History marker: a different one means an outcome was added since (from the phone or synced from another device). */
fun outcomeKey(meta: JsonObject?): String {
    val runs = runs(meta)
    val last = runs.lastOrNull()
    return "${runs.size}:${last?.text("finished").orEmpty()}:${last?.text("job").orEmpty()}"
}

/** Audio in the folder: a WAV longer than its header, or a non-empty file of another audio format. */
fun hasSound(sizes: Map<String, Long>): Boolean = sizes.any { (name, size) ->
    val extension = File(name).extension.lowercase()
    extension in AUDIO_EXTENSIONS && size > (if (extension == "wav") Wav.HEADER else 0)
}

/** [sizes] — folder files (name → size), [meta] — its history, [work] — the phone's running work. */
fun recordState(sizes: Map<String, Long>, meta: JsonObject?, work: Work): RecordState {
    if (work == Work.RUNNING) return RecordState.PROCESSING
    if (work == Work.INTERRUPTED && hasSound(sizes)) return RecordState.UNPROCESSED
    val text = sizes.keys.any { it in TEXTS }
    val run = outcome(meta)
    return when {
        run?.text("status") == "failed" -> RecordState.FAILED
        // text without an outcome in history counts as processed
        text -> RecordState.PROCESSED
        run?.text("status") == "stopped" -> RecordState.AUDIO_ONLY
        run != null -> RecordState.NO_SPEECH
        // nothing to process without audio
        else -> if (hasSound(sizes)) RecordState.UNPROCESSED else RecordState.NO_SPEECH
    }
}
