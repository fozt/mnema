package dev.mnema.app.notify

import dev.mnema.app.helper.HelperState
import dev.mnema.app.helper.HelperStatus
import dev.mnema.app.notify.RecordingNotice.CallPrompt.Step
import dev.mnema.app.notify.RecordingNotice.Notice
import dev.mnema.app.notify.RecordingNotice.Notice.Kind
import dev.mnema.app.ui.record.recordUi
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class RecordingNoticeTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val idle = HelperStatus(v = 1, state = "idle")
    private val recording = HelperStatus(v = 1, state = "recording", folder = "2026-10-21_14-35", startedMs = 1_761_046_500_000)

    @Test
    fun pausedRecordingShowsTheStoppedTimeAndOffersToResume() {
        val status = recording.copy(paused = true, heartbeatMs = recording.startedMs + 90_000, pausedMs = 30_000)

        assertEquals(
            Notice(Kind.RECORDING, recording.startedMs, paused = true, recordedMs = 60_000),
            RecordingNotice.decide(status, alive = true),
        )
        val later = status.copy(heartbeatMs = status.heartbeatMs + 300, pausedMs = 30_300)
        assertEquals("while paused the notification does not change", RecordingNotice.decide(status, true), RecordingNotice.decide(later, true))
    }

    @Test
    fun resumedRecordingTimerLeavesOutThePauses() {
        val status = recording.copy(paused = false, pausedMs = 30_000)

        assertEquals("without pauses — from the recording start", Notice(Kind.RECORDING, recording.startedMs), RecordingNotice.decide(recording, alive = true))
        assertEquals(Notice(Kind.RECORDING, recording.startedMs + 30_000, paused = false), RecordingNotice.decide(status, alive = true))
    }

    @Test
    fun recordingWinsOverCall() {
        val status = recording.copy(callActive = true)

        assertEquals(Kind.RECORDING, RecordingNotice.decide(status, alive = true)?.kind)
    }

    @Test
    fun incomingCallOffersToRecordAndIsGoneOnceDeclined() {
        val ringing = idle.copy(callMode = "ringing")

        assertEquals(Kind.INCOMING, RecordingNotice.decide(ringing, alive = true)?.kind)
        assertEquals("answered", Kind.CALL, RecordingNotice.decide(idle.copy(callMode = "cellular", callActive = true), alive = true)?.kind)
        assertNull("declined", RecordingNotice.decide(idle.copy(callMode = "none"), alive = true))
        assertEquals("the recording wins", Kind.RECORDING, RecordingNotice.decide(recording.copy(callMode = "ringing"), alive = true)?.kind)
        assertNull(RecordingNotice.decide(ringing, alive = false))
    }

    @Test
    fun callPromptTheUserRemovedDoesNotComeBackUntilTheNextCall() {
        val prompt = RecordingNotice.CallPrompt()

        assertEquals("rang", Step.POST, prompt.next(Kind.INCOMING, inShade = false))
        assertEquals("the same check again", Step.KEEP, prompt.next(Kind.INCOMING, inShade = true))
        assertEquals("tapped — the app opened", Step.KEEP, prompt.next(Kind.INCOMING, inShade = false))
        assertEquals("answered", Step.KEEP, prompt.next(Kind.CALL, inShade = false))
        assertEquals("the call ended", Step.CLEAR, prompt.next(null, inShade = false))
        assertEquals("the next call", Step.POST, prompt.next(Kind.CALL, inShade = false))
    }

    @Test
    fun callPromptComesBackOnceARecordingIsStoppedMidCall() {
        val prompt = RecordingNotice.CallPrompt()

        prompt.next(Kind.CALL, inShade = false)
        prompt.next(Kind.RECORDING, inShade = false)
        assertEquals("stopped mid-call", Step.POST, prompt.next(Kind.CALL, inShade = false))
        assertEquals("swiped away", Step.KEEP, prompt.next(Kind.CALL, inShade = false))
        assertEquals(Step.CLEAR, prompt.next(null, inShade = false))
    }

    @Test
    fun callPromptJustPostedStaysWhenTheNextSyncDoesNotSeeItYet() {
        val prompt = RecordingNotice.CallPrompt()

        assertEquals(Step.POST, prompt.next(Kind.INCOMING, inShade = false))
        assertEquals("the system has not posted it yet", Step.KEEP, prompt.next(Kind.INCOMING, inShade = false))
        prompt.next(Kind.RECORDING, inShade = false)
        assertEquals(Step.POST, prompt.next(Kind.CALL, inShade = false))
        assertEquals("\"Recording in progress\" still holds the slot", Step.KEEP, prompt.next(Kind.CALL, inShade = false))
    }

    @Test
    fun callPromptStillInTheShadeFollowsTheCall() {
        val prompt = RecordingNotice.CallPrompt()

        assertEquals(Step.POST, prompt.next(Kind.INCOMING, inShade = false))
        assertEquals("answered — \"Call in progress\" in the same slot", Step.POST, prompt.next(Kind.CALL, inShade = true))
        assertEquals(Step.KEEP, prompt.next(Kind.CALL, inShade = true))
        assertEquals("\"Record\" removed it", Step.KEEP, prompt.next(Kind.CALL, inShade = false))
    }

    @Test
    fun recordingNoticeSaysWhatIsRecordedAndWhenTheOtherSideIsNot() {
        val writing = recording.copy(callSound = true, micSound = true, callSilentMs = 0)
        val blocked = writing.copy(callActive = true, callMode = "cellular", callSound = false, callSilentMs = 6_000)

        val shown = RecordingNotice.decide(writing, alive = true)!!.text!!
        val warned = RecordingNotice.decide(blocked, alive = true)!!.text

        assertNotEquals(shown, warned)
        assertEquals("the same words as on the record screen", recordUi(HelperState.Running(blocked, false), 0, true).warning, warned)
        assertNull("while paused — the time, not the tracks", RecordingNotice.decide(blocked.copy(paused = true), alive = true)!!.text)
        assertNull("the track sound is not known yet", RecordingNotice.decide(recording, alive = true)!!.text)
    }

    @Test
    fun deadHelperShowsNothingEvenIfStatusSaysRecording() {
        assertNull("no status", RecordingNotice.decide(null, alive = true))
        assertNull("the helper is idle", RecordingNotice.decide(idle, alive = true))
        assertNull(RecordingNotice.decide(recording, alive = false))
        assertNull(RecordingNotice.decide(idle.copy(callActive = true), alive = false))
    }

    @Test
    fun endedCallNoticeIsStaleOnceCallResumesOrRecordingStops() {
        assertTrue("the call ended mid-recording", RecordingNotice.callEnded(recording, alive = true))
        assertFalse("the call resumed", RecordingNotice.callEnded(recording.copy(callActive = true), alive = true))
        assertFalse("recording stopped", RecordingNotice.callEnded(idle, alive = true))
        assertFalse(RecordingNotice.callEnded(recording, alive = false))
        assertFalse(RecordingNotice.callEnded(null, alive = true))
    }

    @Test
    fun cancelQuestionReplacesTheRecordingNoticeOfTheSameRecording() {
        val notice = RecordingNotice.decide(recording, alive = true)
        val ask = RecordingNotice.CancelAsk(recording.folder, askedMs = 1_000)

        assertTrue(RecordingNotice.asksCancel(ask, notice, recording.folder, nowMs = 1_000))
        assertTrue(RecordingNotice.asksCancel(ask, notice, recording.folder, nowMs = 60_999))
        assertFalse("the answer did not come in time", RecordingNotice.asksCancel(ask, notice, recording.folder, nowMs = 61_000))
        assertFalse(RecordingNotice.asksCancel(null, notice, recording.folder, nowMs = 1_000))
    }

    @Test
    fun cancelQuestionIsDroppedOnceThatRecordingIsOver() {
        val ask = RecordingNotice.CancelAsk(recording.folder, askedMs = 1_000)
        val next = recording.copy(folder = "2026-10-21_15-00")

        assertFalse("another one is already recording", RecordingNotice.asksCancel(ask, RecordingNotice.decide(next, true), next.folder, nowMs = 2_000))
        assertFalse("recording stopped", RecordingNotice.asksCancel(ask, RecordingNotice.decide(idle, true), "", nowMs = 2_000))
        val call = idle.copy(callActive = true)
        assertFalse(RecordingNotice.asksCancel(ask, RecordingNotice.decide(call, true), recording.folder, nowMs = 2_000))
    }

    private fun root() = File(tmp.root, "Mnema").also { it.mkdirs() }

    private fun track(dir: File, name: String = "mic.wav") {
        dir.mkdirs()
        File(dir, name).writeText("x")
    }

    @Test
    fun stoppedRecordingIsTheLastFolderWithTracks() {
        val root = root()
        track(File(root, "2026-10-21_14-35"))
        val status = idle.copy(lastFolder = "2026-10-21_14-35")

        assertEquals(File(root, "2026-10-21_14-35"), RecordingNotice.stoppedFolder(status, root))
    }

    @Test
    fun nextRecordingAlreadyRunningDoesNotHideTheStoppedOne() {
        val root = root()
        track(File(root, "2026-10-21_14-35"), "system.wav")
        track(File(root, "2026-10-21_15-00"))
        val status = recording.copy(folder = "2026-10-21_15-00", lastFolder = "2026-10-21_14-35")

        assertEquals(File(root, "2026-10-21_14-35"), RecordingNotice.stoppedFolder(status, root))
    }

    @Test
    fun folderThatIsStillBeingWrittenIsNotStopped() {
        val root = root()
        track(File(root, "2026-10-21_14-35"))
        val status = recording.copy(folder = "2026-10-21_14-35", lastFolder = "2026-10-21_14-35")

        assertNull(RecordingNotice.stoppedFolder(status, root))
    }

    @Test
    fun lastFolderWithoutTracksOrOutsideRootIsRejected() {
        val root = root()
        File(root, "empty").mkdirs()
        track(File(tmp.root, "outside"))

        assertNull(RecordingNotice.stoppedFolder(idle.copy(lastFolder = "empty"), root))
        assertNull(RecordingNotice.stoppedFolder(idle.copy(lastFolder = "missing"), root))
        assertNull(RecordingNotice.stoppedFolder(idle.copy(lastFolder = "../outside"), root))
        assertNull(RecordingNotice.stoppedFolder(idle.copy(lastFolder = "/"), root))
        assertNull(RecordingNotice.stoppedFolder(null, root))
        assertNull(RecordingNotice.stoppedFolder(idle, root))
    }
}
