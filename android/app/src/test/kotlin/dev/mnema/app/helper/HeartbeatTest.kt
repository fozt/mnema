package dev.mnema.app.helper

import dev.mnema.app.notify.Notifications
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class HeartbeatTest {
    private val nonce = "0123456789abcdef0123456789abcdef"
    private val sources = Sources()
    private val heartbeat = Heartbeat(4242, nonce, 1_700_000_000_000, sources, Pause())
    private var uptime = 1_000_000L

    private fun status(levels: Heartbeat.Levels? = null): StatusWriter.Status {
        uptime += 300
        return heartbeat.status(1_761_046_533_000 + uptime, uptime, levels)
    }

    private class Levels(var callSound: Boolean? = true, var micSound: Boolean? = true, var callSilentMs: Long? = 0) : Heartbeat.Levels {
        override fun takeCallRms() = 1404.25
        override fun takeMicRms() = 0.0
        override fun micSilenced() = false
        override fun callSound() = callSound
        override fun micSound() = micSound
        override fun callSilentMs() = callSilentMs
        override fun callSource() = CallTrack.POLICY
    }

    private val levels = Levels()

    @Test
    fun pongSurvivesLaterHeartbeatsUntilNextPing() {
        heartbeat.pong("123e4567-e89b-12d3-a456-426614174000")

        repeat(5) { assertEquals("123e4567-e89b-12d3-a456-426614174000", status().pong) }
        heartbeat.starting(1_761_046_500_000, uptime)
        assertEquals("123e4567-e89b-12d3-a456-426614174000", status(levels).pong)

        heartbeat.pong("fedcba98-7654-3210-fedc-ba9876543210")
        assertEquals("fedcba98-7654-3210-fedc-ba9876543210", status(levels).pong)
    }

    @Test
    fun recordingMovesFolderToLastFolderOnStop() {
        heartbeat.starting(1_761_046_500_000, uptime)
        val starting = status(levels)
        assertEquals("recording", starting.state)
        assertEquals("", starting.folder)
        assertEquals(1_761_046_500_000, starting.startedMs)

        heartbeat.started("2026-10-21_14-35")
        val started = status(levels)
        assertEquals("2026-10-21_14-35", started.folder)
        assertEquals(1404.25, started.callRms!!, 0.0)
        assertEquals(false, started.micSilenced)

        heartbeat.stopped(null)
        val stopped = status(levels)
        assertEquals("idle", stopped.state)
        assertEquals("", stopped.folder)
        assertEquals("2026-10-21_14-35", stopped.lastFolder)
        assertEquals("", stopped.error)
        assertNull("no levels when idle", stopped.callRms)
    }

    @Test
    fun pauseIsReportedWithItsTimeAndEndsWithTheRecording() {
        assertFalse("no pause without a recording", heartbeat.pause(true, 1_761_046_400_000))
        assertFalse(status().paused)

        heartbeat.starting(1_761_046_500_000, uptime)
        heartbeat.started("2026-10-22_19-05")
        assertTrue(heartbeat.pause(true, 1_761_046_510_000))
        val paused = heartbeat.status(1_761_046_512_000, uptime, levels)
        assertTrue(paused.paused)
        assertEquals("recording", paused.state)
        assertEquals(2_000L, paused.pausedMs)

        assertTrue(heartbeat.pause(false, 1_761_046_513_000))
        val resumed = heartbeat.status(1_761_046_520_000, uptime, levels)
        assertFalse(resumed.paused)
        assertEquals(3_000L, resumed.pausedMs)

        heartbeat.pause(true, 1_761_046_530_000)
        heartbeat.stopped(null)
        assertFalse(status().paused)
        assertEquals(0L, status().pausedMs)

        heartbeat.starting(1_761_046_600_000, uptime)
        assertFalse("a new recording starts unpaused", status(levels).paused)
    }

    @Test
    fun failedStartKeepsLastFolderAndShowsReason() {
        heartbeat.starting(1_761_046_500_000, uptime)
        heartbeat.started("2026-10-21_14-35")
        heartbeat.stopped(null)
        heartbeat.starting(1_761_046_900_000, uptime)

        heartbeat.stopped(Heartbeat.failure(false, Capture.MIC_UNAVAILABLE))

        val status = status()
        assertEquals("idle", status.state)
        assertEquals("", status.folder)
        assertEquals("2026-10-21_14-35", status.lastFolder)
        assertEquals("Не удалось начать запись: микрофон недоступен", helperText(status.error))

        heartbeat.starting(1_761_046_950_000, uptime)
        assertEquals("a new recording clears the previous error", "", status().error)
    }

    @Test
    fun callStartIsAnnouncedOnlyWithoutRecording() {
        assertEquals("a call started, nothing is recording", Events.CALL_STARTED, heartbeat.call(CallMode.MESSENGER, uptime))
        assertTrue(status().callActive)
        assertNull("the call goes on", heartbeat.call(CallMode.MESSENGER, uptime))
        assertEquals("without a recording the end of a call removes the offer to record", Events.STATE_CHANGED, heartbeat.call(CallMode.NONE, uptime))
        assertFalse(status().callActive)

        heartbeat.starting(1_761_046_500_000, uptime)
        assertNull("already recording", heartbeat.call(CallMode.MESSENGER, uptime))
        assertTrue(status(levels).callActive)
    }

    @Test
    fun callEndIsAnnouncedOncePerEndOnlyWhileRecording() {
        heartbeat.call(CallMode.MESSENGER, uptime)
        heartbeat.starting(1_761_046_500_000, uptime)
        heartbeat.started("2026-10-22_15-10")

        assertEquals(Events.CALL_ENDED, heartbeat.call(CallMode.NONE, uptime + 1000))
        assertNull("the same call end", heartbeat.call(CallMode.NONE, uptime + 1300))
        assertFalse(status(levels).callActive)
        assertNull("the call resumed mid-recording", heartbeat.call(CallMode.MESSENGER, uptime + 5000))
        assertEquals("a new call end", Events.CALL_ENDED, heartbeat.call(CallMode.NONE, uptime + 9000))

        heartbeat.stopped(null)
        heartbeat.call(CallMode.MESSENGER, uptime + 10_000)
        assertEquals("recording stopped: a call end without a recording", Events.STATE_CHANGED, heartbeat.call(CallMode.NONE, uptime + 11_000))

        heartbeat.starting(1_761_046_900_000, uptime + 12_000)
        assertNull(heartbeat.call(CallMode.MESSENGER, uptime + 13_000))
        assertEquals("recording started without a call", Events.CALL_ENDED, heartbeat.call(CallMode.NONE, uptime + 14_000))
    }

    @Test
    fun statusCarriesTrackChoiceThatOutlivesRecordings() {
        val fresh = status()
        assertTrue(fresh.micOn)
        assertTrue(fresh.callOn)

        sources.apply("call-off")
        assertFalse("the choice is visible when idle", status().callOn)
        heartbeat.starting(1_761_046_500_000, uptime)
        sources.apply("mic-off")
        val recording = status(levels)
        assertFalse(recording.micOn)
        assertFalse(recording.callOn)

        heartbeat.stopped(null)
        heartbeat.starting(1_761_046_900_000, uptime)
        val next = status(levels)
        assertFalse("the next recording keeps the choice", next.micOn)
        assertFalse(next.callOn)
    }

    @Test
    fun recordingStartedDuringAnyCallStopsTwoMinutesAfterCallEnds() {
        for (kind in listOf(CallMode.CELLULAR, CallMode.MESSENGER)) {
            heartbeat.call(kind, uptime)
            heartbeat.starting(1_761_046_500_000, uptime)
            assertFalse(kind, heartbeat.autoStop(uptime + 600_000))

            heartbeat.call(CallMode.NONE, uptime)
            assertFalse(kind, heartbeat.autoStop(uptime + 119_999))
            assertTrue(kind, heartbeat.autoStop(uptime + 120_000))
            assertFalse("auto-stop fires once", heartbeat.autoStop(uptime + 120_300))
            heartbeat.stopped(null)
        }
    }

    @Test
    fun incomingCallIsOfferedAndWithdrawnWithoutRecording() {
        assertEquals("the phone rings", Events.STATE_CHANGED, heartbeat.call(CallMode.RINGING, uptime))
        val ringing = status()
        assertEquals(CallMode.RINGING, ringing.callMode)
        assertFalse("no conversation yet", ringing.callActive)
        assertNull(heartbeat.call(CallMode.RINGING, uptime + 300))
        assertEquals("answered", Events.CALL_STARTED, heartbeat.call(CallMode.CELLULAR, uptime + 5_000))
        assertTrue(status().callActive)

        heartbeat.call(CallMode.NONE, uptime + 60_000)
        assertEquals("ringing again", Events.STATE_CHANGED, heartbeat.call(CallMode.RINGING, uptime + 70_000))
        assertEquals("declined", Events.STATE_CHANGED, heartbeat.call(CallMode.NONE, uptime + 75_000))
        assertEquals(CallMode.NONE, status().callMode)
    }

    @Test
    fun recordingStartedOnIncomingCallStopsTwoMinutesAfterTheCall() {
        heartbeat.call(CallMode.RINGING, uptime)
        heartbeat.starting(1_761_046_500_000, uptime)
        assertNull("answered while recording", heartbeat.call(CallMode.CELLULAR, uptime + 5_000))
        assertFalse(heartbeat.autoStop(uptime + 300_000))

        assertEquals(Events.CALL_ENDED, heartbeat.call(CallMode.NONE, uptime + 300_000))
        assertTrue(heartbeat.autoStop(uptime + 420_000))

        heartbeat.stopped(null)
        heartbeat.call(CallMode.RINGING, uptime + 500_000)
        heartbeat.starting(1_761_046_900_000, uptime + 500_000)
        assertEquals("the incoming call the recording started in was declined", Events.CALL_ENDED, heartbeat.call(CallMode.NONE, uptime + 510_000))
        assertTrue(heartbeat.autoStop(uptime + 630_000))
    }

    @Test
    fun declinedIncomingCallDoesNotEndARecordingStartedWithoutCall() {
        heartbeat.starting(1_761_046_500_000, uptime)
        heartbeat.started("2026-10-25_10-00")
        assertNull(heartbeat.call(CallMode.RINGING, uptime + 1_000))
        assertNull("there was no conversation", heartbeat.call(CallMode.NONE, uptime + 9_000))
        assertFalse(heartbeat.autoStop(uptime + 600_000))
    }

    @Test
    fun soundChangeOfARecordingIsAnnouncedOncePerChange() {
        val sound = Levels(callSound = false, micSound = true, callSilentMs = 1_000)
        assertFalse("not recording", heartbeat.soundChanged(sound))
        heartbeat.starting(1_761_046_500_000, uptime)
        assertFalse("no folder yet", heartbeat.soundChanged(sound))
        heartbeat.started("2026-10-25_10-00")
        assertFalse("the first check is not a change", heartbeat.soundChanged(sound))
        assertFalse(heartbeat.soundChanged(sound))

        sound.callSound = true
        assertTrue("the other side is heard", heartbeat.soundChanged(sound))
        assertFalse(heartbeat.soundChanged(sound))

        heartbeat.call(CallMode.CELLULAR, uptime)
        sound.callSound = false
        sound.callSilentMs = HelperProtocol.SILENT_CALL_MS - 1
        assertTrue(heartbeat.soundChanged(sound))
        sound.callSilentMs = HelperProtocol.SILENT_CALL_MS
        assertTrue("5 s of zeros in a call: the call sound is not recorded", heartbeat.soundChanged(sound))
        sound.callSilentMs = HelperProtocol.SILENT_CALL_MS + 30_000
        assertFalse("the zeros go on", heartbeat.soundChanged(sound))

        heartbeat.stopped(null)
        assertFalse("recording stopped", heartbeat.soundChanged(sound))
    }

    @Test
    fun recordingStatusCarriesSoundOfEachTrackAndTheCallSource() {
        heartbeat.call(CallMode.CELLULAR, uptime)
        heartbeat.starting(1_761_046_500_000, uptime)
        heartbeat.started("2026-10-25_10-00")
        val status = status(Levels(callSound = false, micSound = true, callSilentMs = 7_000))

        assertEquals(CallMode.CELLULAR, status.callMode)
        assertEquals(false, status.callSound)
        assertEquals(true, status.micSound)
        assertEquals(7_000L, status.callSilentMs)
        assertEquals(CallTrack.POLICY, status.callSource)
        heartbeat.stopped(null)
        assertNull("no track sound when idle", status(levels).callSound)
    }

    @Test
    fun callResumedWithinGraceKeepsRecording() {
        heartbeat.call(CallMode.MESSENGER, uptime)
        heartbeat.starting(1_761_046_500_000, uptime)
        heartbeat.call(CallMode.NONE, uptime)
        heartbeat.call(CallMode.MESSENGER, uptime + 60_000)

        assertFalse(heartbeat.autoStop(uptime + 200_000))
        heartbeat.call(CallMode.NONE, uptime + 300_000)
        assertTrue(heartbeat.autoStop(uptime + 420_000))
    }

    @Test
    fun recordingStartedOutsideCallIsNotStoppedByCall() {
        heartbeat.starting(1_761_046_500_000, uptime)
        heartbeat.call(CallMode.MESSENGER, uptime)
        heartbeat.call(CallMode.NONE, uptime + 1000)

        assertFalse(heartbeat.autoStop(uptime + 600_000))
    }

    @Test
    fun interruptedRecordingBecomesLastAndItsStopIsAnnouncedOnce() {
        assertFalse("no interrupted recording", heartbeat.takeInterrupted())

        heartbeat.interrupted("2026-10-22_14-05")

        val first = status()
        assertEquals("idle", first.state)
        assertEquals("", first.folder)
        assertEquals("2026-10-22_14-05", first.lastFolder)
        assertTrue(heartbeat.takeInterrupted())
        assertFalse("the stop is announced once", heartbeat.takeInterrupted())
        assertEquals("2026-10-22_14-05", status().lastFolder)
    }

    @Test
    fun recordingIsAnnouncedAgainEveryMinute() {
        assertFalse("not recording", heartbeat.noticeDue(uptime + 600_000))
        heartbeat.starting(1_761_046_500_000, uptime)
        assertFalse("the recording has not started yet", heartbeat.noticeDue(uptime + 60_000))
        heartbeat.started("2026-10-21_14-35")

        assertFalse(heartbeat.noticeDue(uptime + 59_999))
        assertTrue(heartbeat.noticeDue(uptime + 60_000))
        assertFalse(heartbeat.noticeDue(uptime + 60_300))
        assertTrue(heartbeat.noticeDue(uptime + 120_000))

        heartbeat.stopped(null)
        assertFalse("recording stopped", heartbeat.noticeDue(uptime + 600_000))
    }

    @Test
    fun liveHelperRenewsRecordingNotificationBeforeItExpires() {
        assertTrue(Heartbeat.RECORDING_NOTICE_MS * 2 < Notifications.RECORDING_TIMEOUT_MS)
    }
}
