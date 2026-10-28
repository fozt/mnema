package dev.mnema.app.notify

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import dev.mnema.app.helper.HelperCommand

class NotificationActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        val app = context.applicationContext
        // the button removes its notification at once, without waiting for the helper
        Notifications.dismissFrom(app, intent)
        Notifications.handle(this) {
            when (action) {
                ACTION_START -> Notifications.sendCommand(app, HelperCommand.START)
                ACTION_STOP -> {
                    Notifications.clearCallEnded(app)
                    Notifications.sendCommand(app, HelperCommand.STOP)
                }
                ACTION_CONTINUE -> Notifications.clearCallEnded(app)
                ACTION_DISMISSED -> Notifications.sync(app)
                ACTION_PAUSE, ACTION_RESUME -> {
                    Notifications.sendCommand(app, if (action == ACTION_PAUSE) HelperCommand.PAUSE else HelperCommand.RESUME)
                    Notifications.sync(app)
                }
                ACTION_CANCEL_ASK -> intent.getStringExtra(Notifications.EXTRA_FOLDER)?.let { Notifications.askCancel(app, it) }
                ACTION_CANCEL_KEEP -> Notifications.keepRecording(app)
                ACTION_CANCEL -> intent.getStringExtra(Notifications.EXTRA_FOLDER)?.let { Notifications.cancelRecording(app, it) }
            }
        }
    }

    companion object {
        const val ACTION_START = "dev.mnema.app.notify.START"
        const val ACTION_STOP = "dev.mnema.app.notify.STOP"
        const val ACTION_DISMISSED = "dev.mnema.app.notify.RECORDING_DISMISSED"
        const val ACTION_CONTINUE = "dev.mnema.app.notify.CONTINUE"
        const val ACTION_PAUSE = "dev.mnema.app.notify.PAUSE"
        const val ACTION_RESUME = "dev.mnema.app.notify.RESUME"
        const val ACTION_CANCEL_ASK = "dev.mnema.app.notify.CANCEL_ASK"
        const val ACTION_CANCEL = "dev.mnema.app.notify.CANCEL"
        const val ACTION_CANCEL_KEEP = "dev.mnema.app.notify.CANCEL_KEEP"
    }
}
