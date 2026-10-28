package dev.mnema.app.notify

import dev.mnema.app.helper.HelperStatus
import dev.mnema.app.transcript.RecordingFolder
import dev.mnema.app.ui.record.soundLine
import java.io.File

object RecordingNotice {
    /**
     * [startedMs] — what the timer runs from; [paused] — the recording is paused; [recordedMs] — recorded by the pause; [text] — what
     * the tracks of the recording in progress carry.
     */
    data class Notice(val kind: Kind, val startedMs: Long, val paused: Boolean = false, val recordedMs: Long = 0, val text: String? = null) {
        enum class Kind { CALL, INCOMING, RECORDING }
    }

    /** null: nothing to show. A recording beats a call, a call beats an incoming one; a call has no timer; while paused the timer stands. */
    fun decide(status: HelperStatus?, alive: Boolean): Notice? = when {
        status == null || !alive -> null
        status.recording && status.paused == true ->
            Notice(Notice.Kind.RECORDING, status.startedMs, paused = true, recordedMs = status.recordedMs(status.heartbeatMs))
        status.recording -> Notice(Notice.Kind.RECORDING, status.startedMs + status.pausedMs, paused = status.paused, text = soundLine(status))
        status.callActive -> Notice(Notice.Kind.CALL, 0)
        status.ringing -> Notice(Notice.Kind.INCOMING, 0)
        else -> null
    }

    /** The "Cancel recording?" question in the notification about recording [folder]; [askedMs] — by the clock since boot. */
    data class CancelAsk(val folder: String, val askedMs: Long)

    /** Ask about cancelling instead of "Recording": the same recording that was asked about goes on, and the answer is awaited for at most a minute. */
    fun asksCancel(ask: CancelAsk?, notice: Notice?, folder: String, nowMs: Long): Boolean =
        ask != null && notice?.kind == Notice.Kind.RECORDING && folder.isNotEmpty() && ask.folder == folder &&
            nowMs - ask.askedMs in 0 until CANCEL_ASK_MS

    private const val CANCEL_ASK_MS = 60_000L

    /**
     * Offer to record a call: shown once per call and updated while it is in the shade; one removed by a tap, a swipe or a
     * button without recording does not come back until the next call. A recording stopped in the middle of a call shows the
     * offer again.
     */
    class CallPrompt {
        private var posted: Notice.Kind? = null

        /** What the check does with the call and recording slot; a recording in progress takes it with "Recording". */
        enum class Step { POST, KEEP, CLEAR }

        /**
         * Check against what to show: [kind] — from [decide], [inShade] — the offer is visible in the shade. While a call goes on
         * the slot is not cleared: the system posts a just-shown offer with a delay, and the next check does not see it.
         */
        fun next(kind: Notice.Kind?, inShade: Boolean): Step = when (kind) {
            null -> Step.CLEAR.also { posted = null }
            Notice.Kind.RECORDING -> Step.KEEP.also { posted = null }
            else -> if (posted == null || inShade && posted != kind) Step.POST.also { posted = kind } else Step.KEEP
        }
    }

    /** "Call ended" when it fits: the recording goes on, the call does not. */
    fun callEnded(status: HelperStatus?, alive: Boolean): Boolean =
        alive && status != null && status.recording && !status.callActive

    /** Folder of the just-stopped recording ready for processing; null — nothing to take. */
    fun stoppedFolder(status: HelperStatus?, root: File): File? {
        val name = status?.lastFolder?.takeIf { it.isNotEmpty() } ?: return null
        val writing = status.folder.takeIf { status.recording && it.isNotEmpty() }
        val dir = File(root, name)
        return dir.takeIf { RecordingFolder.isCandidate(it, root, writing) }
    }
}
