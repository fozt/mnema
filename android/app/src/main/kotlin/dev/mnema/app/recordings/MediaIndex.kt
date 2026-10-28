package dev.mnema.app.recordings

import java.io.File
import java.io.IOException

/**
 * Android's file index (MediaProvider) for shared storage. It lets a folder be renamed only when it knows all of its
 * files, and a file replaced through a temporary name (atomicWrite, atomicReplace) does not always reach the index: the
 * replacement is done but the index insert fails ("UNIQUE constraint failed: files._data"). With transcript.txt and
 * summary.txt on disk but missing from the index, Android refused to rename both the section and the recording (EPERM).
 * So every replaced file is indexed at once, and a refused folder move is retried once after reindexing its files.
 * The app sets [scanner] (MediaScannerConnection); JVM tests have none, so there is nothing to index.
 */
object MediaIndex {
    /** Indexes files; wait — block until done (before retrying a move). */
    @Volatile var scanner: ((files: List<File>, wait: Boolean) -> Unit)? = null

    /** A file was just replaced: index it without waiting. */
    fun replaced(file: File) {
        scanner?.invoke(listOf(file), false)
    }

    /** A file was just renamed: the old path leaves the index, the new one enters it. */
    fun moved(from: File, to: File) {
        scanner?.invoke(listOf(from, to), false)
    }

    /** Moves folder [dir]; if Android refuses and the folder is still there, indexes its files and tries once more. */
    fun <T> retrying(dir: File, move: () -> T): T = try {
        move()
    } catch (e: IOException) {
        val scan = scanner
        if (scan == null || !dir.isDirectory) throw e
        scan(dir.walkTopDown().filter { it.isFile }.toList(), true)
        move()
    }
}
