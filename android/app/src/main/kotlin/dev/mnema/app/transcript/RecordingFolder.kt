package dev.mnema.app.transcript

import dev.mnema.app.i18n.tr
import dev.mnema.app.recordings.MediaIndex
import dev.mnema.app.recordings.recordingDirectory
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.LinkOption.NOFOLLOW_LINKS
import java.nio.file.StandardCopyOption

/** All or nothing: a temporary `.mnema-*.tmp` file alongside, fsync, then replace. */
internal fun atomicWrite(target: File, content: ByteArray) {
    val temporary = File.createTempFile(".mnema-${target.name.trimStart('.')}.", ".tmp", target.absoluteFile.parentFile)
    try {
        FileOutputStream(temporary).use {
            it.write(content)
            it.fd.sync()
        }
        try {
            Files.move(temporary.toPath(), target.toPath(), StandardCopyOption.ATOMIC_MOVE)
        } catch (_: AtomicMoveNotSupportedException) {
            Files.move(temporary.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING)
        }
        MediaIndex.replaced(target)
    } finally {
        temporary.delete()
    }
}

/** Recording folder files in the main format, as the desktop reads them (`is_recording_folder`, `recent()`). */
object RecordingFolder {
    const val TRANSCRIPT = "transcript.txt"
    const val SUMMARY = "summary.txt"
    const val LEGACY_SUMMARY = "summary.md" // earlier versions; becomes summary.txt when the recording is read

    /**
     * summary.md of earlier versions becomes summary.txt the first time the recording is read; an existing summary.txt
     * wins. True when the file was renamed.
     */
    fun migrateSummary(dir: File): Boolean {
        val old = File(dir, LEGACY_SUMMARY)
        val new = File(dir, SUMMARY)
        if (!Files.isRegularFile(old.toPath(), NOFOLLOW_LINKS) || Files.exists(new.toPath(), NOFOLLOW_LINKS)) return false
        return try {
            Files.move(old.toPath(), new.toPath()) // never over an existing file
            MediaIndex.moved(old, new)
            true
        } catch (_: IOException) { false } // the next read tries again
    }

    /** Track names, like the desktop SOURCE_NAMES (live/remarks.py), in bubble order: the other side left, me right. */
    val TRACK_NAMES: LinkedHashMap<String, String>
        get() = linkedMapOf("system" to tr("System audio"), "mic" to tr("Microphone"))

    /** A direct child of the root with a track that the helper is not writing right now. */
    fun isCandidate(dir: File, root: File, recordingNow: String?): Boolean = try {
        val canonical = recordingDirectory(root, dir)
        canonical != null && canonical.relativeTo(root.canonicalFile).invariantSeparatorsPath != recordingNow && canonical.absolutePath != recordingNow &&
            (File(canonical, "mic.wav").isFile || File(canonical, "system.wav").isFile)
    } catch (_: IOException) {
        false
    }
}
