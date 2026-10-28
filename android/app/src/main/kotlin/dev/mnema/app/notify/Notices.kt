package dev.mnema.app.notify

import dev.mnema.app.Redact
import dev.mnema.app.i18n.tr
import dev.mnema.app.notify.NotificationActionReceiver.Companion.ACTION_CANCEL
import dev.mnema.app.notify.NotificationActionReceiver.Companion.ACTION_CANCEL_ASK
import dev.mnema.app.notify.NotificationActionReceiver.Companion.ACTION_CANCEL_KEEP
import dev.mnema.app.notify.NotificationActionReceiver.Companion.ACTION_CONTINUE
import dev.mnema.app.notify.NotificationActionReceiver.Companion.ACTION_PAUSE
import dev.mnema.app.notify.NotificationActionReceiver.Companion.ACTION_RESUME
import dev.mnema.app.notify.NotificationActionReceiver.Companion.ACTION_START
import dev.mnema.app.notify.NotificationActionReceiver.Companion.ACTION_STOP
import dev.mnema.app.ui.record.CANCEL_LABEL
import dev.mnema.app.ui.record.CANCEL_NOT_DELETED
import dev.mnema.app.ui.record.CANCEL_TEXT
import dev.mnema.app.ui.record.CANCEL_TITLE
import dev.mnema.app.ui.record.KEEP_LABEL
import dev.mnema.app.ui.record.recordingClock
import java.io.File

/** Where a tap on the notification leads. */
internal sealed interface NoticeTap {
    data object None : NoticeTap
    data object App : NoticeTap
    data class Recording(val result: ResultNotice) : NoticeTap
}

/** A notification button: [action] — what the receiver does, [dismisses] — whether it removes its notification, [folder] — whose recording it is. */
internal data class NoticeButton(val label: String, val action: String, val dismisses: Boolean, val folder: String? = null)

/** A notification without Android API: [autoCancel] — a tap removes it from the shade, [ongoing] — it stays while its subject goes on. */
internal data class NoticeSpec(
    val title: String,
    val text: String? = null,
    val tap: NoticeTap = NoticeTap.App,
    val autoCancel: Boolean,
    val ongoing: Boolean = false,
    val buttons: List<NoticeButton> = emptyList(),
)

/** What app notifications show: those opening the app go away on tap and on their button, the recording one stays while it goes on. */
internal object Notices {
    private val CALL_TITLE: String get() = tr("Call in progress")
    private val INCOMING_TITLE: String get() = tr("Incoming call")
    private val RECORDING_TITLE: String get() = tr("Recording in progress")
    private val PAUSED_TITLE: String get() = tr("Recording paused")
    private val PAUSE_LABEL: String get() = tr("Pause")
    private val RESUME_LABEL: String get() = tr("Resume")
    private val START_LABEL: String get() = tr("Record")
    private val STOP_LABEL: String get() = tr("Stop")
    private val CALL_ENDED_TITLE: String get() = tr("Call ended")
    private val STOP_RECORDING_LABEL: String get() = tr("Stop recording")
    private val CONTINUE_LABEL: String get() = tr("Continue")
    private val DONE_TITLE: String get() = tr("Transcript ready")
    private val FAILED_TITLE: String get() = tr("Processing failed")
    private val START_HELPER_TITLE: String get() = tr("Start the helper")

    /** A call is going on or the phone rings, nothing is recording: offer to record. */
    fun call(incoming: Boolean) = NoticeSpec(
        title = if (incoming) INCOMING_TITLE else CALL_TITLE,
        autoCancel = true,
        buttons = listOf(NoticeButton(START_LABEL, ACTION_START, dismisses = true)),
    )

    /** "Recording" or "Recording paused" with the time it stopped at; [folder] — what "Cancel recording" cancels. */
    fun recording(notice: RecordingNotice.Notice, folder: String): NoticeSpec {
        val toggle = if (notice.paused) NoticeButton(RESUME_LABEL, ACTION_RESUME, dismisses = false)
        else NoticeButton(PAUSE_LABEL, ACTION_PAUSE, dismisses = false)
        return NoticeSpec(
            title = if (notice.paused) PAUSED_TITLE else RECORDING_TITLE,
            text = if (notice.paused) recordingClock(notice.recordedMs) else notice.text,
            autoCancel = false,
            ongoing = true,
            buttons = listOfNotNull(
                toggle,
                NoticeButton(STOP_LABEL, ACTION_STOP, dismisses = false),
                NoticeButton(CANCEL_LABEL, ACTION_CANCEL_ASK, dismisses = false, folder = folder).takeIf { folder.isNotEmpty() },
            ),
        )
    }

    /** In place of "Recording": the question about recording [folder]; the answer brings "Recording" back or removes it. */
    fun cancelAsk(folder: String) = NoticeSpec(
        title = CANCEL_TITLE,
        text = CANCEL_TEXT,
        autoCancel = false,
        ongoing = true,
        buttons = listOf(
            NoticeButton(CANCEL_LABEL, ACTION_CANCEL, dismisses = false, folder = folder),
            NoticeButton(KEEP_LABEL, ACTION_CANCEL_KEEP, dismisses = false),
        ),
    )

    /** A cancelled recording was not deleted: a tap opens it. */
    fun cancelNotDeleted(result: ResultNotice, reason: String?) = NoticeSpec(
        title = CANCEL_NOT_DELETED,
        text = reason?.let(Redact::redact),
        tap = NoticeTap.Recording(result),
        autoCancel = true,
    )

    /** The call ended, the recording goes on: offer to stop it. */
    fun callEnded() = NoticeSpec(
        title = CALL_ENDED_TITLE,
        autoCancel = true,
        buttons = listOf(
            NoticeButton(STOP_RECORDING_LABEL, ACTION_STOP, dismisses = true),
            NoticeButton(CONTINUE_LABEL, ACTION_CONTINUE, dismisses = true),
        ),
    )

    fun processingDone(result: ResultNotice, title: String) = NoticeSpec(
        title = DONE_TITLE,
        text = Redact.redact(title).ifBlank { File(result.folder).name },
        tap = NoticeTap.Recording(result),
        autoCancel = true,
    )

    fun processingFailed(result: ResultNotice, message: String) = NoticeSpec(
        title = FAILED_TITLE,
        text = Redact.redact(message),
        tap = NoticeTap.Recording(result),
        autoCancel = true,
    )

    /** The helper did not start after a self-update. */
    fun startHelper() = NoticeSpec(title = START_HELPER_TITLE, autoCancel = true)
}
