package dev.mnema.app.recordings

import dev.mnema.app.i18n.tr
import dev.mnema.app.sync.filesLock
import dev.mnema.app.sync.hidden
import dev.mnema.app.sync.syncDirectory
import java.io.File
import java.io.IOException
import java.nio.file.Files

private val MOVE_FAILED: String get() = tr("Could not move the recording")
private val GONE: String get() = tr("The recording folder is unavailable")
private val NAME_TAKEN: String get() = tr("This name is taken")
private val NO_NAME: String get() = tr("Enter a section name")
private val SECTION_GONE: String get() = tr("The section no longer exists")

/**
 * Moves a finished recording from the root into section [section]; a taken name gets " (2)". [busy] — recordings being
 * written or processed. No section, a busy recording, or one already in a section — the same folder, no error.
 */
internal fun moveToSection(root: File, dir: File, section: String, busy: Set<String>, sections: Set<String> = emptySet()): File =
    synchronized(filesLock) {
        if (section.isEmpty()) return dir
        try {
            val base = root.canonicalFile
            val source = recordingDirectory(base, dir)?.takeIf { it.parentFile == base && isLibraryRecord(base, it, sections) } ?: return dir
            if (source.absolutePath in busy || source.name in busy || recordingId(source) in busy) return dir
            val home = sectionDirectory(base, section, sections) ?: return dir
            relocate(base, source, home)
        } catch (_: IOException) { dir }
    }

/** A new section name by the folder-name rule (desktop folder_title, at most 240 bytes); empty means it cannot be created. */
fun sectionName(text: String): String = sectionTitle(text)

/**
 * "Move to section": recording [dir] goes to root section [section], or to the root when empty; the id and files stay,
 * a taken name gets " (2)". [create] — "New section…": the name is cleaned by the folder-name rule, a section with the
 * same name in another case is reused, otherwise it is created. Already there — the same folder. The caller checks
 * busyness under the same filesLock. Errors are IOExceptions with a user-facing text; the recording stays in place.
 */
internal fun moveRecording(root: File, dir: File, section: String, sections: Set<String> = emptySet(), create: Boolean = false): File =
    synchronized(filesLock) {
        val base = root.canonicalFile
        val source = recordingDirectory(base, dir)?.takeIf { isLibraryRecord(base, it, sections) } ?: throw IOException(GONE)
        var created: File? = null
        val home = when {
            create -> {
                val name = sectionName(section).ifEmpty { throw IOException(NO_NAME) }
                // phone storage is case-insensitive: "work" and "Work" are one folder
                val existing = (base.list() ?: throw IOException(MOVE_FAILED)).firstOrNull { !hidden(it) && it.equals(name, ignoreCase = true) }
                if (existing != null) {
                    sectionDirectory(base, existing, sections) ?: throw IOException(NAME_TAKEN)
                } else {
                    File(base, name).also { if (!it.mkdir()) throw IOException(MOVE_FAILED); created = it }
                }
            }
            section.isEmpty() -> base
            else -> sectionDirectory(base, section, sections) ?: throw IOException(SECTION_GONE)
        }
        if (source.parentFile == home) return source
        try {
            relocate(base, source, home)
        } catch (e: IOException) {
            // a section created for nothing does not stay as an empty folder
            created?.delete()
            throw IOException(MOVE_FAILED, e)
        }
    }

// no REPLACE_EXISTING: a name taken meanwhile fails instead of being replaced; no section is created
private fun relocate(base: File, source: File, home: File): File {
    val target = freeName(File(home, source.name))
    MediaIndex.retrying(source) { Files.move(source.toPath(), target.toPath()) }
    // the root too, in case a section was created in it
    try { setOf(source.parentFile!!, home, base).forEach(::syncDirectory) } catch (_: IOException) { /* already moved */ }
    return target
}
