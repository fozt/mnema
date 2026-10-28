package dev.mnema.app.update

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import dev.mnema.app.AppGraph
import dev.mnema.app.MnemaApp
import dev.mnema.app.launch.LaunchResult
import dev.mnema.app.notify.Notifications
import dev.mnema.app.ui.record.helperClient
import dev.mnema.app.ui.record.startHelper
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

// the worst Kadb start takes about 16 s; the system waits longer for a background broadcast
internal const val LAUNCH_TIMEOUT_MS = 20_000L
private const val LOG_TAG = "MnemaUpdate"

/** Starts the helper after a self-update; true — it did not start, a notification is needed. Without pairing there is nothing to start with. */
internal suspend fun relaunchFailed(
    paired: Boolean,
    timeoutMs: Long,
    onError: (Exception) -> Unit = {},
    launch: suspend () -> LaunchResult,
): Boolean {
    if (!paired) return false
    return try {
        withTimeoutOrNull(timeoutMs) { launch() } != LaunchResult.Started
    } catch (e: CancellationException) {
        throw e
    } catch (e: Exception) {
        onError(e)
        true
    }
}

/** The helper with the previous APK exited: the new one starts the same way as the "Start" button. */
class PackageReplacedReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        val graph = MnemaApp.graph(context)
        val pending = goAsync()
        graph.scope.launch {
            try {
                // Kadb is not interrupted by cancellation: wait for the start separately to fit the receiver deadline
                val failed = relaunchFailed(graph.launcher.paired, LAUNCH_TIMEOUT_MS,
                    onError = { Log.w(LOG_TAG, "Helper not started after the update", it) }) {
                    graph.scope.async { launchHelper(graph) }.await()
                }
                if (failed) Notifications.showStartHelper(context)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Log.w(LOG_TAG, "Helper not started after the update", e)
                Notifications.showStartHelper(context)
            } finally {
                pending.finish()
            }
        }
    }

    private suspend fun launchHelper(graph: AppGraph): LaunchResult {
        val client = helperClient(graph).getOrElse { return LaunchResult.Failed(it.message.orEmpty()) }
        return startHelper(graph, client)
    }
}
