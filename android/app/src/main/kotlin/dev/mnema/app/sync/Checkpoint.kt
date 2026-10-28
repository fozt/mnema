package dev.mnema.app.sync

import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.NoSuchFileException

fun loadCheckpoint(file: File): Checkpoint = try {
    wire.decodeFromString(Checkpoint.serializer(), utf8(Files.readAllBytes(file.toPath()))).also { state ->
        require(state.records.all { (id, record) -> id == record.id })
    }
} catch (_: NoSuchFileException) { Checkpoint() }
catch (_: IllegalArgumentException) { Checkpoint() }
catch (_: java.nio.charset.CharacterCodingException) { Checkpoint() }

fun saveCheckpoint(file: File, state: Checkpoint) {
    Files.createDirectories(file.absoluteFile.parentFile!!.toPath())
    atomicReplace(file, (wire.encodeToString(Checkpoint.serializer(), state) + "\n").toByteArray(), private = true)
}

/** The folder's transcript and summary match the last sync with [server]: the audio can go, the texts stay there. */
internal fun textsOnServer(root: File, dir: File, state: Checkpoint, server: String): Boolean {
    if (state.server != server) return false
    val base = root.absoluteFile.normalize()
    val local = try { readRecord(base, dir.absoluteFile.normalize().relativeTo(base).invariantSeparatorsPath) }
        catch (_: IOException) { return false }
        catch (_: IllegalArgumentException) { return false }
    val known = state.records[local.id] ?: return false
    val texts = listOf(FileName.TRANSCRIPT, FileName.SUMMARY)
    return texts.any { it in local.files } && texts.all { local.files[it]?.sha256 == known.files[it]?.sha256 }
}
