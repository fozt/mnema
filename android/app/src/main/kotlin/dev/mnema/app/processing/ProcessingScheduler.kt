package dev.mnema.app.processing

import java.io.File

interface ProcessingScheduler {
    /** false — the recording was not accepted: no track, or no transcript for a summary. */
    fun enqueue(dir: File, summaryOnly: Boolean = false): Boolean

    /** "Process again" with a chosen range; the folder that will be processed (a sibling for a part), null — refused. */
    fun reprocess(dir: File, options: ReprocessOptions): File?

    fun cancel(dir: File)
}
