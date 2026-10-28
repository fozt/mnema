package dev.mnema.app.processing

import android.content.Context
import android.util.Log
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import dev.mnema.app.MnemaApp
import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.Redact
import dev.mnema.app.notify.Notifications
import dev.mnema.app.recordings.recordingTitle
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** One processing step of a folder by id; the token and keys come from settings, Data does not carry them. */
class ProcessWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val id = inputData.getString(KEY_ID) ?: return Result.success()
        val graph = MnemaApp.graph(applicationContext)
        // sync separates copies with one id: the work belongs to the folder it leaves the id with
        val (syncing, previous) = withContext(Dispatchers.IO) {
            graph.sync.enabled().let { on -> on to (if (on) graph.sync.previousPath(id) else null) }
        }
        val find = { root: File, record: String -> WorkScheduler.findFolder(root, record, syncing, previous) }
        val dir = withContext(Dispatchers.IO) { find(graph.recordingsRoot, id) } ?: return Result.success()
        val settings = graph.settings.current()
        val recording = graph.recordingFolder()
        val pipeline = Pipeline(
            settings = settings,
            runner = graph.runner(settings),
            encoder = MediaCodecEncoder(),
            cacheDir = File(applicationContext.cacheDir, CACHE_DIR),
            clock = System::currentTimeMillis,
            recordingNow = { recording },
            log = { Log.i(LOG_TAG, it) },
            findFolder = find,
        )
        val (result, folder) = withContext(Dispatchers.IO) {
            val held = graph.processingStarted(dir)
            try {
                pipeline.step(dir, System.currentTimeMillis() + STEP_MS) { graph.reportProgress(dir, it) }
            } finally {
                graph.reportProgress(dir, null)
                graph.processingFinished(held)
            }
        }
        graph.recordings.refresh()
        when (result) {
            is StepResult.Again -> WorkScheduler(applicationContext).next(id, result.delayMs)
            StepResult.Done -> Notifications.processingDone(applicationContext, folder, withContext(Dispatchers.IO) { recordingTitle(folder) })
            // audio only is neither a result nor a failure: nothing to report
            StepResult.AudioOnly -> Unit
            is StepResult.Failed -> Notifications.processingFailed(applicationContext, folder, Redact.redact(result.message))
        }
        return Result.success()
    }

    companion object {
        const val KEY_ID = "id"
        private const val CACHE_DIR = "processing"
        private const val STEP_MS = 8 * 60_000L
        private const val LOG_TAG = "MnemaProcess"
    }
}
