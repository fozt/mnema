package dev.mnema.app.notify

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import dev.mnema.app.MnemaApp
import dev.mnema.app.helper.HelperProtocol
import java.io.File

class HelperEventReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        val app = context.applicationContext
        Notifications.handle(this) {
            try {
                if (action == HelperProtocol.ACTION_RECORDING_STARTED) MnemaApp.graph(app).recordingStarted()
                if (action == HelperProtocol.ACTION_RECORDING_STOPPED) {
                    enqueueStopped(app)
                    MnemaApp.graph(app).wakeSync()
                }
            } finally {
                Notifications.sync(app, callEnded = action == HelperProtocol.ACTION_CALL_ENDED)
            }
        }
    }

    // also for a cut-off recording: the new helper sends RECORDING_STOPPED with its folder in last_folder; a cancelled one is not processed
    private suspend fun enqueueStopped(context: Context) {
        val folder = RecordingNotice.stoppedFolder(Notifications.lastStatus(context), File(HelperProtocol.RECORDINGS_ROOT)) ?: return
        val graph = MnemaApp.graph(context)
        graph.recordingStopped(folder)?.let { graph.processing.enqueue(it) }
    }
}
