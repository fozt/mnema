package dev.mnema.app.notify

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.graphics.drawable.Icon
import android.os.SystemClock
import android.util.Log
import dev.mnema.app.MainActivity
import dev.mnema.app.MnemaApp
import dev.mnema.app.R
import dev.mnema.app.helper.HelperClient
import dev.mnema.app.helper.HelperCommand
import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.helper.HelperState
import dev.mnema.app.helper.HelperStatus
import dev.mnema.app.i18n.tr
import dev.mnema.app.settings.sourceChoice
import dev.mnema.app.ui.record.CancelOutcome
import dev.mnema.app.ui.record.RecordingCancel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull
import java.io.File
import java.io.IOException
import java.util.concurrent.atomic.AtomicReference

private const val LOG_TAG = "MnemaNotify"

object Notifications {
    const val EXTRA_FOLDER = "folder"
    const val EXTRA_ID = "id"
    const val EXTRA_FILE_KEY = "file_key"
    private const val EXTRA_NOTICE_ID = "notice_id"
    private const val EXTRA_NOTICE_TAG = "notice_tag"

    private const val CHANNEL_CALLS = "calls"
    private const val CHANNEL_RECORDING = "recording"
    private const val CHANNEL_PROCESSING = "processing"
    private const val STATE_ID = 7001
    private const val RESULT_ID = 7002
    private const val CALL_ENDED_ID = 7003
    // "Start the helper" after a self-update
    private const val START_HELPER_ID = 7004
    private const val CANCEL_FAILED_ID = 7005
    private const val REQUEST_OPEN = 1
    private const val REQUEST_RESULT = 5
    // actions differ by their action and by the notification they remove
    private const val REQUEST_ACTION = 0
    private const val STATE_WAIT_MS = HelperProtocol.PING_WAIT_MS + 2000
    // the helper reports the end of a call; if it didn't, it is stopped itself and the offer to record fades on its own
    private const val CALL_PROMPT_TIMEOUT_MS = 3 * 60 * 1000L
    // the helper's auto-stop after a call waits as long
    internal const val CALL_ENDED_TIMEOUT_MS = 2 * 60 * 1000L
    // the helper repeats RECORDING_STARTED every minute: without it "Recording in progress" fades after 3 min
    internal const val RECORDING_TIMEOUT_MS = 180_000L

    private val CALLS_NAME: String get() = tr("Calls")
    private val RECORDING_NAME: String get() = tr("Recording")
    private val PROCESSING_NAME: String get() = tr("Processing")
    private const val NO_HELPER = "No connection to the helper"

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val syncLock = Mutex()
    private val cancelAsk = AtomicReference<RecordingNotice.CancelAsk?>(null)
    // checked only under syncLock
    private val callPrompt = RecordingNotice.CallPrompt()

    fun ensureChannels(context: Context) {
        val manager = manager(context)
        manager.createNotificationChannel(NotificationChannel(CHANNEL_CALLS, CALLS_NAME, NotificationManager.IMPORTANCE_HIGH))
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_RECORDING, RECORDING_NAME, NotificationManager.IMPORTANCE_LOW).apply {
                setSound(null, null)
                enableVibration(false)
                setShowBadge(false)
            },
        )
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_PROCESSING, PROCESSING_NAME, NotificationManager.IMPORTANCE_DEFAULT),
        )
    }

    private fun showCall(context: Context, incoming: Boolean) = post(context, CHANNEL_CALLS, STATE_ID, Notices.call(incoming)) {
        setTimeoutAfter(CALL_PROMPT_TIMEOUT_MS)
        setOnlyAlertOnce(true)
    }

    /** The offer to record is still in the shade: it was not removed by a tap, a swipe or a button. */
    private fun callPromptInShade(context: Context): Boolean = manager(context).activeNotifications.any {
        it.id == STATE_ID && it.tag == null && it.notification.channelId == CHANNEL_CALLS
    }

    /** "Recording in progress" with a timer, or "Recording paused" with the time it stopped at. */
    private fun showRecording(context: Context, notice: RecordingNotice.Notice, folder: String) =
        post(context, CHANNEL_RECORDING, STATE_ID, Notices.recording(notice, folder)) {
            setTimeoutAfter(RECORDING_TIMEOUT_MS)
            setOnlyAlertOnce(true)
            setDeleteIntent(broadcast(context, NotificationActionReceiver.ACTION_DISMISSED))
            if (notice.paused) {
                setShowWhen(false)
            } else {
                setWhen(if (notice.startedMs > 0) notice.startedMs else System.currentTimeMillis())
                setShowWhen(true)
                setUsesChronometer(true)
            }
        }

    /** The question was swiped away — the recording goes on. */
    private fun showCancelAsk(context: Context, folder: String) = post(context, CHANNEL_RECORDING, STATE_ID, Notices.cancelAsk(folder)) {
        setTimeoutAfter(RECORDING_TIMEOUT_MS)
        setOnlyAlertOnce(true)
        setShowWhen(false)
        setDeleteIntent(broadcast(context, NotificationActionReceiver.ACTION_CANCEL_KEEP))
    }

    /** "Cancel recording" in the notification: first a question in its place. */
    internal suspend fun askCancel(context: Context, folder: String) {
        cancelAsk.set(RecordingNotice.CancelAsk(folder, SystemClock.elapsedRealtime()))
        sync(context)
    }

    /** "Continue recording" or a swiped-away question: "Recording in progress" again. */
    internal suspend fun keepRecording(context: Context) {
        cancelAsk.set(null)
        sync(context)
    }

    /** Confirmed cancellation of recording [folder]; if it was not deleted — a separate notification, a tap opens the recording. */
    internal suspend fun cancelRecording(context: Context, folder: String) {
        cancelAsk.set(null)
        clearCallEnded(context)
        val graph = MnemaApp.graph(context)
        val outcome = RecordingCancel.cancel(graph, folder)
        if (outcome is CancelOutcome.NotDeleted) showCancelNotDeleted(context, File(graph.recordingsRoot, folder), outcome.reason)
        sync(context)
    }

    private fun showCancelNotDeleted(context: Context, dir: File, reason: String?) =
        post(context, CHANNEL_RECORDING, CANCEL_FAILED_ID, Notices.cancelNotDeleted(ResultNotice.of(dir), reason))

    /** Removes both "Recording in progress" and "Call in progress": they share one slot. */
    fun clearRecording(context: Context) = manager(context).cancel(STATE_ID)

    private fun showCallEnded(context: Context) = post(context, CHANNEL_CALLS, CALL_ENDED_ID, Notices.callEnded()) {
        setTimeoutAfter(CALL_ENDED_TIMEOUT_MS)
    }

    fun clearCallEnded(context: Context) = manager(context).cancel(CALL_ENDED_ID)

    /** Notification from `status.json`; returns at once. */
    fun syncWithStatus(context: Context) {
        val app = context.applicationContext
        scope.launch { sync(app) }
    }

    fun processingDone(context: Context, dir: File, title: String) {
        val result = ResultNotice.of(dir)
        showResult(context, result, Notices.processingDone(result, title))
    }

    fun processingFailed(context: Context, dir: File, message: String) {
        val result = ResultNotice.of(dir)
        showResult(context, result, Notices.processingFailed(result, message))
    }

    private fun showResult(context: Context, result: ResultNotice, spec: NoticeSpec) =
        post(context, CHANNEL_PROCESSING, RESULT_ID, spec, tag = result.tag) {
            setVisibility(Notification.VISIBILITY_PRIVATE)
            setPublicVersion(publicVersion(context, spec.title))
        }

    /** "Start the helper" after a self-update; [sync] removes it once the helper starts. */
    fun showStartHelper(context: Context) = post(context, CHANNEL_PROCESSING, START_HELPER_ID, Notices.startHelper())

    /** [callEnded] — CALL_ENDED arrived: show "Call ended" if it still fits. */
    internal suspend fun sync(context: Context, callEnded: Boolean = false) = syncLock.withLock {
        val (status, alive) = liveStatus(context)
        if (alive) manager(context).cancel(START_HELPER_ID)
        val notice = RecordingNotice.decide(status, alive)
        val folder = status?.folder.orEmpty()
        val ask = cancelAsk.get()
        val asking = RecordingNotice.asksCancel(ask, notice, folder, SystemClock.elapsedRealtime())
        // a stale question is forgotten; one asked meanwhile is not
        if (!asking && ask != null) cancelAsk.compareAndSet(ask, null)
        val kind = notice?.kind
        val calling = kind == RecordingNotice.Notice.Kind.CALL || kind == RecordingNotice.Notice.Kind.INCOMING
        val step = callPrompt.next(kind, inShade = calling && callPromptInShade(context))
        when {
            notice?.kind == RecordingNotice.Notice.Kind.RECORDING -> if (asking) showCancelAsk(context, folder) else showRecording(context, notice, folder)
            step == RecordingNotice.CallPrompt.Step.POST -> showCall(context, incoming = kind == RecordingNotice.Notice.Kind.INCOMING)
            step == RecordingNotice.CallPrompt.Step.CLEAR -> clearRecording(context)
        }
        when {
            !RecordingNotice.callEnded(status, alive) -> clearCallEnded(context)
            callEnded -> showCallEnded(context)
        }
    }

    /** status.json as is, without the heartbeat check: the folder of a stopped recording is needed even when the helper stops. */
    internal fun lastStatus(context: Context): HelperStatus? = helper(context)?.readStatus()

    /** Before "Record" the helper gets the track choice from settings. */
    internal suspend fun sendCommand(context: Context, command: HelperCommand) {
        val client = helper(context) ?: return
        val choice = if (command == HelperCommand.START) MnemaApp.graph(context).settings.current().sourceChoice() else null
        try {
            client.command(command, choice)
        } catch (e: IOException) {
            Log.w(LOG_TAG, "Command ${command.wire} not sent", e)
        }
    }

    internal fun handle(receiver: BroadcastReceiver, block: suspend () -> Unit) {
        val pending = receiver.goAsync()
        scope.launch {
            try {
                block()
            } catch (e: Exception) {
                Log.w(LOG_TAG, "Receiver failed", e)
            } finally {
                pending.finish()
            }
        }
    }

    private suspend fun liveStatus(context: Context): Pair<HelperStatus?, Boolean> {
        val client = helper(context) ?: return null to false
        val state = withTimeoutOrNull(STATE_WAIT_MS) { client.state.first { it !is HelperState.Unknown } }
        return if (state is HelperState.Running) state.status to true else null to false
    }

    private fun helper(context: Context): HelperClient? = try {
        HelperClient.forContext(context, scope)
    } catch (e: Exception) {
        Log.w(LOG_TAG, NO_HELPER, e)
        null
    }

    private fun manager(context: Context): NotificationManager = context.getSystemService(NotificationManager::class.java)

    /** A notification from [spec]; [more] — what the spec lacks: timer, timeout, public version. */
    private fun post(
        context: Context,
        channel: String,
        id: Int,
        spec: NoticeSpec,
        tag: String? = null,
        more: Notification.Builder.() -> Unit = {},
    ) {
        val builder = noticeBuilder(context, channel, spec, tapIntent(context, spec.tap)).setCategory(Notification.CATEGORY_STATUS)
        for (button in spec.buttons) {
            val intent = Intent(context, NotificationActionReceiver::class.java).setAction(button.action)
            button.folder?.let { intent.putExtra(EXTRA_FOLDER, it) }
            if (button.dismisses) dismissing(intent, id, tag)
            builder.addAction(action(context, button.label, receiverIntent(context, intent)))
        }
        builder.more()
        manager(context).notify(tag, id, builder.build())
    }

    /** The notification base from the spec: title, text, tap; whether a tap removes it comes from [spec]. */
    internal fun noticeBuilder(context: Context, channel: String, spec: NoticeSpec, tap: PendingIntent?): Notification.Builder {
        val builder = Notification.Builder(context, channel)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(spec.title)
            .setAutoCancel(spec.autoCancel)
            .setOngoing(spec.ongoing)
        spec.text?.let(builder::setContentText)
        tap?.let(builder::setContentIntent)
        return builder
    }

    internal fun action(context: Context, label: String, intent: PendingIntent): Notification.Action =
        Notification.Action.Builder(Icon.createWithResource(context, R.drawable.ic_launcher_foreground), label, intent).build()

    /** The button removes notification [id] with [tag]: the receiver of its intent calls [dismissFrom]. */
    internal fun dismissing(intent: Intent, id: Int, tag: String?): Intent = intent
        .putExtra(EXTRA_NOTICE_ID, id)
        .putExtra(EXTRA_NOTICE_TAG, tag)
        .setIdentifier("notice:$id:${tag.orEmpty()}")

    /** Removes the notification whose button sent [intent]. */
    internal fun dismissFrom(context: Context, intent: Intent) {
        val id = intent.getIntExtra(EXTRA_NOTICE_ID, 0)
        if (id != 0) manager(context).cancel(intent.getStringExtra(EXTRA_NOTICE_TAG), id)
    }

    private fun tapIntent(context: Context, tap: NoticeTap): PendingIntent? = when (tap) {
        NoticeTap.None -> null
        NoticeTap.App -> openApp(context)
        is NoticeTap.Recording -> openRecording(context, tap.result)
    }

    private fun publicVersion(context: Context, title: String): Notification =
        Notification.Builder(context, CHANNEL_PROCESSING)
            .setSmallIcon(R.drawable.ic_notification)
            .setCategory(Notification.CATEGORY_STATUS)
            .setContentTitle(title)
            .build()

    private fun broadcast(context: Context, name: String): PendingIntent =
        receiverIntent(context, Intent(context, NotificationActionReceiver::class.java).setAction(name))

    // extras (the recording, the notification to remove) are updated in place
    private fun receiverIntent(context: Context, intent: Intent): PendingIntent =
        PendingIntent.getBroadcast(context, REQUEST_ACTION, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    internal fun openApp(context: Context): PendingIntent = PendingIntent.getActivity(
        context,
        REQUEST_OPEN,
        Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE,
    )

    // a separate identifier per processing: PendingIntents differ regardless of extras
    private fun openRecording(context: Context, result: ResultNotice): PendingIntent {
        val intent = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .setIdentifier(result.tag)
            .putExtra(EXTRA_FOLDER, result.folder)
        result.id?.let { intent.putExtra(EXTRA_ID, it) }
        result.fileKey?.let { intent.putExtra(EXTRA_FILE_KEY, it) }
        return PendingIntent.getActivity(
            context,
            REQUEST_RESULT,
            intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }
}
