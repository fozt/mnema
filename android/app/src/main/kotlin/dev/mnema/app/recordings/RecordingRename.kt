package dev.mnema.app.recordings

import dev.mnema.app.i18n.tr
import dev.mnema.app.processing.ProcessStage
import dev.mnema.app.processing.ProcessStateFile
import dev.mnema.app.sync.RENAMING
import dev.mnema.app.sync.filesLock
import dev.mnema.app.sync.syncDirectory
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.LinkOption
import java.time.ZoneId

private val RENAME_FAILED: String get() = tr("Could not rename the recording")
private val ENDED: String get() = tr("Processing has already finished. Rename the recording again")

/**
 * Renames idle recording [dir] to [title]: the folder name follows the desktop folder_name (fixture folder-names-v1) —
 * the start date, the part mark kept, an empty title gives the time name. A taken name gets " (2)"; another folder is
 * never replaced.
 */
internal fun renameRecording(dir: File, title: String, zone: ZoneId = ZoneId.systemDefault()): File = synchronized(filesLock) {
    val parent = dir.absoluteFile.parentFile ?: throw IOException(RENAME_FAILED)
    val target = freeName(File(parent, folderName(recordingStart(dir, zone).at, title, partMark(dir.name))), dir)
    if (target.name != dir.name) {
        try { moveFolder(dir, target) } catch (e: IOException) { throw IOException(RENAME_FAILED, e) }
        try { syncDirectory(parent) } catch (_: IOException) { /* already renamed */ }
    }
    // a title set earlier is no longer pending
    ProcessStateFile.read(target)?.takeIf { it.userTitle != null }?.let { state ->
        try { ProcessStateFile.write(target, state.copy(userTitle = null)) } catch (_: IOException) { /* the folder is already named */ }
    }
    target
}

/**
 * Sets a title for a recording being processed: the folder stays as is and processing names it at the end; empty
 * clears it. Throws IOException when processing is no longer running.
 */
internal fun nameProcessing(dir: File, title: String) = synchronized(filesLock) {
    val state = ProcessStateFile.own(dir)?.takeIf { it.stage in ProcessStage.UNFINISHED } ?: throw IOException(ENDED)
    ProcessStateFile.write(dir, state.copy(userTitle = chosenTitle(title, partMark(dir.name))))
}

/** Moves a recording folder to free name [to] (freeName); the same name in another case goes through [changeCase]. No REPLACE_EXISTING: a name taken meanwhile fails. */
internal fun moveFolder(from: File, to: File) {
    if (!sameFolder(to, from) || !Files.exists(to.toPath(), LinkOption.NOFOLLOW_LINKS)) {
        MediaIndex.retrying(from) { Files.move(from.toPath(), to.toPath()) }
        return
    }
    changeCase(from, to)
}

/**
 * A case-only rename is two real renames through a temporary name, never direct: on shared storage, after a direct case
 * change MediaProvider kept the helper's file rows (com.android.shell) under the old name and then refused every rename
 * inside the folder (EPERM). On failure the old name comes back, or the library scan restores it.
 */
internal fun changeCase(
    from: File,
    to: File,
    step: (File, File) -> Unit = { a, b -> MediaIndex.retrying(a) { Files.move(a.toPath(), b.toPath()) } },
) {
    val temp = File(from.parentFile, RENAMING + to.name)
    step(from, temp)
    try {
        step(temp, to)
    } catch (e: IOException) {
        try { step(temp, from) } catch (_: IOException) { /* the library scan restores the folder */ }
        throw e
    }
}
