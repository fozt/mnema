package dev.mnema.app.sync

import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.NoSuchFileException
import dev.mnema.app.transcript.RecordingFolder
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

fun loadCheckpoint(file: File): Checkpoint = try {
    val json = wire.parseToJsonElement(utf8(Files.readAllBytes(file.toPath())))
    wire.decodeFromJsonElement(Checkpoint.serializer(), withSummaryTxt(json)).also { state ->
        require(state.records.all { (id, record) -> id == record.id })
    }
} catch (_: NoSuchFileException) { Checkpoint() }
catch (_: IllegalArgumentException) { Checkpoint() }
catch (_: java.nio.charset.CharacterCodingException) { Checkpoint() }

/** summary.md of an earlier sync is summary.txt now: the scan renames the file and keeps its bytes and time, so the rename is no change on either side. */
private fun withSummaryTxt(state: JsonElement): JsonElement {
    val records = (state as? JsonObject)?.get("records") as? JsonObject ?: return state
    return JsonObject(state + ("records" to JsonObject(records.mapValues { (_, element) ->
        val record = element as? JsonObject ?: return@mapValues element
        val files = record["files"] as? JsonObject ?: return@mapValues record
        val legacy = files[RecordingFolder.LEGACY_SUMMARY] ?: return@mapValues record
        JsonObject(record + ("files" to JsonObject(mapOf(FileName.SUMMARY.fileName to legacy) + files - RecordingFolder.LEGACY_SUMMARY)))
    })))
}

fun saveCheckpoint(file: File, state: Checkpoint) {
    Files.createDirectories(file.absoluteFile.parentFile!!.toPath())
    atomicReplace(file, (wire.encodeToString(Checkpoint.serializer(), state) + "\n").toByteArray(), private = true)
}

/** foreign after a new device token at the address of the last sync: whose library the server has is unknown until its list. */
internal const val UNCHECKED_LIBRARY = "?"

internal fun markUnchecked(file: File, server: String) {
    val state = loadCheckpoint(file)
    if (state.server == server && state.foreign != UNCHECKED_LIBRARY) saveCheckpoint(file, state.copy(foreign = UNCHECKED_LIBRARY))
}

/** The folder's transcript and summary match the last sync with [server], and its library is still there: the audio can go, the texts stay there. */
internal fun textsOnServer(root: File, dir: File, state: Checkpoint, server: String): Boolean {
    if (state.server != server || state.foreign.isNotEmpty()) return false
    val base = root.absoluteFile.normalize()
    val local = try { readRecord(base, dir.absoluteFile.normalize().relativeTo(base).invariantSeparatorsPath) }
        catch (_: IOException) { return false }
        catch (_: IllegalArgumentException) { return false }
    val known = state.records[local.id] ?: return false
    val texts = listOf(FileName.TRANSCRIPT, FileName.SUMMARY)
    return texts.any { it in local.files } && texts.all { local.files[it]?.sha256 == known.files[it]?.sha256 }
}
