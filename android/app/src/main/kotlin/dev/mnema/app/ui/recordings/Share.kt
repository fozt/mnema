package dev.mnema.app.ui.recordings

import android.content.ClipData
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.util.Log
import androidx.core.content.FileProvider
import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.recordings.RecordingDetail
import dev.mnema.app.transcript.RecordingFolder
import java.io.File
import java.io.IOException
import java.time.LocalDate
import java.time.ZoneId
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** A recording file to send: [name] is the copy's name, by which it is recognized in a chat or notes. */
data class SharedFile(val source: File, val name: String, val type: String)

/** Something to share: a transcript with lines or a composed summary. */
fun shareable(detail: RecordingDetail): Boolean = detail.dialog.isNotEmpty() || detail.summary != null

/** Non-empty transcript.txt and summary.md of the recording; the copy's name starts with the folder name. */
fun sharedFiles(dir: File): List<SharedFile> = listOf(
    SharedFile(File(dir, RecordingFolder.TRANSCRIPT), "${dir.name}_transcript.txt", TEXT_PLAIN),
    SharedFile(File(dir, RecordingFolder.SUMMARY), "${dir.name}_summary.md", TEXT_MARKDOWN),
).filter { it.source.isFile && it.source.length() > 0 }

/** The helper's report on the recording and the last lines of its log, if any; without a report there is nothing to share. */
fun reportFiles(dir: File): List<SharedFile> {
    if (!File(dir, HelperProtocol.REPORT).isFile) return emptyList()
    return listOf(
        SharedFile(File(dir, HelperProtocol.REPORT), "${dir.name}_helper-report.json", APPLICATION_JSON),
        SharedFile(File(dir, HelperProtocol.LOG_TAIL), "${dir.name}_helper-log.txt", TEXT_PLAIN),
    ).filter { it.source.isFile && it.source.length() > 0 }
}

/** Opens the chooser for where to send the recording files; false when sharing failed. */
suspend fun shareRecording(context: Context, detail: RecordingDetail): Boolean = share(context, detail) { sharedFiles(it) }

/** The same for the helper's report on the recording: it is examined when audio is not recorded on someone else's phone. */
suspend fun shareReport(context: Context, detail: RecordingDetail): Boolean = share(context, detail) { reportFiles(it) }

private suspend fun share(context: Context, detail: RecordingDetail, files: (File) -> List<SharedFile>): Boolean {
    val uris = try {
        withContext(Dispatchers.IO) { copies(context, files(detail.item.dir)) }
    } catch (e: IOException) {
        Log.w(LOG_TAG, "Copy for sending not written", e)
        return false
    }
    if (uris.isEmpty()) return false
    val send = if (uris.size == 1) {
        Intent(Intent.ACTION_SEND).setType(uris.single().second).putExtra(Intent.EXTRA_STREAM, uris.single().first)
    } else {
        Intent(Intent.ACTION_SEND_MULTIPLE).setType(if (uris.all { it.second.startsWith(TEXT) }) TEXT_ANY else ANY)
            .putParcelableArrayListExtra(Intent.EXTRA_STREAM, ArrayList(uris.map { it.first }))
    }
    val zone = ZoneId.systemDefault()
    val label = recordingLabel(detail.item, LocalDate.now(zone), zone, relative = false)
    send.putExtra(Intent.EXTRA_SUBJECT, label).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    send.clipData = ClipData.newUri(context.contentResolver, label, uris.first().first).apply {
        uris.drop(1).forEach { addItem(ClipData.Item(it.first)) }
    }
    return try {
        context.startActivity(Intent.createChooser(send, null))
        true
    } catch (e: RuntimeException) {
        Log.w(LOG_TAG, "Sharing failed", e)
        false
    }
}

// cacheDir/share/ holds only the copies of the last send: the recording folder may be renamed while it is read
private fun copies(context: Context, files: List<SharedFile>): List<Pair<Uri, String>> {
    val dir = File(context.cacheDir, SHARE_DIR)
    dir.listFiles()?.forEach { it.delete() }
    if (!dir.isDirectory && !dir.mkdirs()) throw IOException("Folder not created: $dir")
    return files.map { file ->
        val copy = file.source.copyTo(File(dir, file.name), overwrite = true)
        FileProvider.getUriForFile(context, "${context.packageName}$AUTHORITY_SUFFIX", copy) to file.type
    }
}

private const val SHARE_DIR = "share"
private const val AUTHORITY_SUFFIX = ".share"
private const val TEXT_PLAIN = "text/plain"
private const val TEXT_MARKDOWN = "text/markdown"
private const val TEXT = "text/"
private const val TEXT_ANY = "text/*"
private const val APPLICATION_JSON = "application/json"
private const val ANY = "*/*"
private const val LOG_TAG = "MnemaShare"
