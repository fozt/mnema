package dev.mnema.app.processing

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import dev.mnema.app.MnemaApp
import dev.mnema.app.server.RunnerClient
import dev.mnema.app.server.RunnerException
import dev.mnema.app.settings.NodeEntry
import dev.mnema.app.settings.Settings

/** Cancels the runner job of a stopped processing: a short work when the network is up. */
class RunnerCancelWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val job = inputData.getString(KEY_JOB) ?: return Result.success()
        val node = inputData.getString(KEY_NODE) ?: return Result.success()
        val graph = MnemaApp.graph(applicationContext)
        cancellationNode(graph.settings.current(), node)?.let { entry ->
            graph.runner(entry)?.let { cancelJob(it, job) }
        }
        return Result.success()
    }

    companion object {
        const val KEY_JOB = "job"
        const val KEY_NODE = "node"
    }
}

/** Best effort: the job already ended or the runner is unreachable — nothing to cancel. */
internal suspend fun cancelJob(runner: RunnerClient, job: String) {
    try {
        runner.cancel(job)
    } catch (_: RunnerException) {
        // the job on the runner will finish on its own
    }
}

/** The cancellation goes to the previous node, even if the user chose another one. */
internal fun cancellationNode(settings: Settings, address: String): NodeEntry? =
    settings.nodes.firstOrNull { it.address == address }
