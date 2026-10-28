package dev.mnema.app.ui

import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.helper.HelperState
import dev.mnema.app.helper.HelperStatus
import dev.mnema.app.helper.SourceChoice
import dev.mnema.app.ui.record.Source
import dev.mnema.app.ui.record.TrackSound
import dev.mnema.app.ui.record.recordUi
import dev.mnema.app.ui.record.soundLine
import dev.mnema.app.ui.record.trackSound
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class TrackSoundTest {
    private val now = 1_761_046_500_000L
    private val recording = HelperStatus(
        v = 1, state = "recording", folder = "2026-10-25_10-00", startedMs = now - 60_000,
        callSound = true, micSound = true, callSilentMs = 0,
    )
    private val inCall = recording.copy(callActive = true, callMode = "cellular")

    private fun ui(status: HelperStatus, choice: SourceChoice? = null) = recordUi(HelperState.Running(status, false), now, true, choice = choice)

    @Test
    fun arrivingSoundIsWritten() {
        assertEquals(TrackSound.WRITING, trackSound(inCall, Source.CALL))
        assertEquals(TrackSound.WRITING, trackSound(inCall, Source.MIC))
        assertNull(ui(inCall).warning)
    }

    @Test
    fun zerosOfTheOtherSideDuringACallBecomeAnErrorAfterFiveSeconds() {
        val zeros = inCall.copy(callSound = false)

        assertEquals("пауза в разговоре", TrackSound.SILENCE, trackSound(zeros.copy(callSilentMs = HelperProtocol.SILENT_CALL_MS - 1), Source.CALL))
        val blocked = zeros.copy(callSilentMs = HelperProtocol.SILENT_CALL_MS)
        assertEquals(TrackSound.BLOCKED, trackSound(blocked, Source.CALL))
        val shown = ui(blocked)
        assertEquals(TrackSound.BLOCKED, shown.callSound)
        assertNotNull(shown.warning)
        assertNotNull("чем вызвано", shown.warningDetail)
        assertEquals("в уведомлении — то же предупреждение", shown.warning, soundLine(blocked))
    }

    @Test
    fun silenceWithoutACallIsNeutral() {
        val quiet = recording.copy(callSound = false, callSilentMs = 600_000)

        assertEquals(TrackSound.SILENCE, trackSound(quiet, Source.CALL))
        assertNull(ui(quiet).warning)
        assertNull(ui(quiet).warningDetail)
    }

    @Test
    fun microphoneIsWrittenOrSilentAndItsBusyWarningStays() {
        val busy = inCall.copy(micSound = false, micSilenced = true, callSilentMs = 60_000)

        assertEquals("микрофон не бывает ошибкой звонка", TrackSound.SILENCE, trackSound(busy, Source.MIC))
        assertEquals(TrackSound.SILENCE, ui(busy).micSound)
        assertNotNull("занят — как прежде", ui(busy).warning)
    }

    @Test
    fun nothingIsSaidWhenUnknownPausedOrSwitchedOff() {
        assertNull("звук дорожки ещё не известен", trackSound(recording.copy(callSound = null), Source.CALL))
        assertNull(trackSound(recording.copy(paused = true), Source.CALL))
        assertNull(trackSound(recording.copy(callOn = false), Source.CALL))
        assertNull("запись ещё не началась", trackSound(recording.copy(folder = ""), Source.MIC))
        assertNull(trackSound(HelperStatus(v = 1, state = "idle", callSound = true), Source.CALL))
        assertNull("выключили на экране", ui(recording, SourceChoice(call = false)).callSound)
        assertNull(soundLine(recording.copy(paused = true)))
    }
}
