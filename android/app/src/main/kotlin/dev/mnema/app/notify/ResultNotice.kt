package dev.mnema.app.notify

import dev.mnema.app.recordings.recordingId
import java.io.File

/** Notification of a processing result: [tag] — the library id of the recording, it survives a folder rename. */
internal data class ResultNotice(val tag: String, val folder: String, val id: String?, val fileKey: String?) {
    companion object {
        fun of(dir: File): ResultNotice {
            val id = recordingId(dir)?.takeIf { it.isNotEmpty() }
            return ResultNotice(tag = id ?: dir.absolutePath, folder = dir.absolutePath, id = id, fileKey = dev.mnema.app.ui.recordings.folderFileKey(dir))
        }
    }
}
