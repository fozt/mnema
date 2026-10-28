package dev.mnema.app.recordings

import dev.mnema.app.i18n.tr
import dev.mnema.app.processing.ProcessStage
import dev.mnema.app.processing.ProcessStateFile
import dev.mnema.app.sync.Kind
import dev.mnema.app.sync.RENAMING
import dev.mnema.app.sync.filesLock
import dev.mnema.app.sync.hidden
import dev.mnema.app.sync.kind
import dev.mnema.app.transcript.Meta
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.NoSuchFileException
import kotlinx.serialization.json.JsonPrimitive

// renaming — recording folders under a temporary case-change name
internal data class LibraryFolders(val records: List<File>, val sections: List<String>, val renaming: List<File> = emptyList())

/** Only a recording in the root or in one section; the folder name does not define the recording's identity. */
internal fun recordingDirectory(root: File, path: File): File? = try {
    val dir = path.canonicalFile
    val depth = depth(root.canonicalFile, dir)
    dir.takeIf { depth in 1..2 && it.isDirectory && (depth == 1 || kindOf(it.parentFile!!, 1) == Kind.SECTION) }
} catch (_: IOException) { null }

/** A recording by the sync rule: a section with stray files does not become one. sections — sections of the last sync. */
internal fun isLibraryRecord(root: File, dir: File, sections: Set<String> = emptySet()): Boolean = try {
    val safe = recordingDirectory(root, dir)
    val depth = safe?.let { depth(root.canonicalFile, it) } ?: 0
    safe != null && kindOf(safe, depth, known = depth == 1 && safe.name in sections) == Kind.RECORD
} catch (_: IOException) { false }

/** Folder of section [name] in the root by the scan rule; null when missing, a recording, or a link. */
internal fun sectionDirectory(root: File, name: String, sections: Set<String> = emptySet()): File? =
    try { section(root, name, sections) } catch (_: IOException) { null }

/**
 * The section [name] for new recordings is gone, like the desktop _record_section: missing or now a recording — the
 * recording stays in the root and the choice resets to no section. An unreadable root or section folder is not gone.
 */
internal fun sectionGone(root: File, name: String, sections: Set<String> = emptySet()): Boolean = name.isNotEmpty() && try {
    entries(root.canonicalFile, 0)
    section(root, name, sections) == null
} catch (_: IOException) { false }

// IOException — the root or folder is unreadable, so whether it is a section is unknown
private fun section(root: File, name: String, sections: Set<String>): File? {
    val dir = File(root.canonicalFile, name)
    val plain = name.isNotEmpty() && !hidden(name) && '/' !in name && '\u0000' !in name
    return dir.takeIf { plain && it.canonicalFile == it && it.isDirectory && kindOf(it, 1, known = name in sections) == Kind.SECTION }
}

/** No UI limit. A failed scan never becomes an empty library. Hidden folders are neither sections nor recordings. */
internal fun libraryFolders(root: File, sections: Set<String> = emptySet()): LibraryFolders {
    if (!root.exists()) return LibraryFolders(emptyList(), emptyList())
    val base = root.canonicalFile
    val seen = mutableSetOf<File>()
    val renaming = mutableListOf<File>()
    fun folders(parent: File, level: Int): List<File> = entries(parent, level - 1).orEmpty()
        .onEach { if (it.name.startsWith(RENAMING)) renaming += it }
        .filter { !hidden(it.name) && it.isDirectory }.sortedBy { it.name }.mapNotNull { folder ->
            val dir = folder.canonicalFile
            if (!dir.toPath().startsWith(base.toPath())) throw IOException(tr("The recording path is outside the Mnema folder"))
            dir.takeIf { depth(base, it) == level && seen.add(it) } // a link to a library folder is not a second recording
        }
    val records = mutableListOf<File>()
    val found = mutableListOf<String>()
    for (dir in folders(base, 1)) {
        when (kindOf(dir, 1, known = dir.name in sections)) {
            Kind.SECTION -> { found += dir.name; folders(dir, 2).filterTo(records) { kindOf(it, 2) == Kind.RECORD } }
            Kind.RECORD -> records += dir
            Kind.OTHER, null -> {}
        }
    }
    return LibraryFolders(records, found, renaming)
}

// the same rule as the sync scan (fixture library-layout-v1); null — skip the folder
private fun kindOf(dir: File, depth: Int, known: Boolean = false): Kind? {
    val (folders, files) = (entries(dir, depth) ?: return null).partition { it.isDirectory }
    return kind(depth, files.map { it.name }.toSet(), folders.map { it.name }, known)
}

/**
 * Contents of a folder at [depth] (0 — the root). null — the folder vanished during the scan (renamed at the end of
 * processing, moved by sync) or a recording in a section is unreadable. Throws for an unreadable root or a folder in
 * it, since that folder may be a section.
 */
private fun entries(dir: File, depth: Int): List<File>? = try {
    Files.newDirectoryStream(dir.toPath()).use { stream -> stream.map { it.toFile() } }
} catch (e: IOException) {
    if (depth == 0 || (depth == 1 && e !is NoSuchFileException)) throw IOException(tr("The recordings folder is unavailable"), e) else null
}

// 1 — a folder in the root, 2 — in a section; 0 — outside the root or hidden
private fun depth(base: File, dir: File): Int {
    if (dir == base || !dir.toPath().startsWith(base.toPath())) return 0
    val parts = base.toPath().relativize(dir.toPath())
    return if (parts.any { hidden(it.toString()) }) 0 else parts.nameCount
}

internal fun recordingId(dir: File): String? = (Meta.read(dir)?.get("id") as? JsonPrimitive)?.takeIf { it.isString }?.content
internal fun findRecording(root: File, id: String): File? = libraryFolders(root).records.singleOrNull { recordingId(it) == id }
// the desktop core.base.AUDIO_EXT, the phone's only set of audio formats (fixtures audio-cleanup-v1, recording-start-v1)
internal val AUDIO_EXTENSIONS = setOf("wav", "flac", "m4a", "mp3", "ogg", "aac", "opus", "wma")

/**
 * An unfinished state covers the gap before WorkManager enqueues; unknown work is busy, someone else's (a copy) is not.
 * Work superseded by an outcome written without it keeps the recording busy only while it has WorkManager work, as in
 * resume.
 */
internal fun busyRecordings(root: File, recording: String?, hasWork: (String) -> Boolean, active: Set<String> = emptySet()): Set<String> = synchronized(filesLock) {
    val result = active.toMutableSet()
    recording?.takeIf { it.isNotEmpty() }?.let { result += it; result += File(root, it).absolutePath }
    for (dir in libraryFolders(root).records) {
        val unknown = File(dir, ProcessStateFile.FILE).exists() && ProcessStateFile.read(dir) == null
        val state = ProcessStateFile.own(dir)
        val unfinished = state?.stage in ProcessStage.UNFINISHED && ProcessStateFile.current(dir) != null
        if (unknown || state != null && (unfinished || hasWork(state.id))) {
            result += dir.relativeTo(root.canonicalFile).invariantSeparatorsPath
            result += dir.absolutePath
            recordingId(dir)?.let(result::add)
        }
    }
    result
}
