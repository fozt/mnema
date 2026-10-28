package dev.mnema.app.helper

import android.media.AudioManager
import org.junit.Assert.assertEquals
import org.junit.Test

class CallModeTest {
    @Test
    fun everyCallModeIsACallAndRingtoneIsAnIncomingCall() {
        for (mode in listOf(AudioManager.MODE_IN_CALL, AudioManager.MODE_CALL_SCREENING, AudioManager.MODE_CALL_REDIRECT)) {
            assertEquals("mode $mode", CallMode.CELLULAR, CallMode.of(mode))
        }
        for (mode in listOf(AudioManager.MODE_IN_COMMUNICATION, AudioManager.MODE_COMMUNICATION_REDIRECT)) {
            assertEquals("mode $mode", CallMode.MESSENGER, CallMode.of(mode))
        }
        assertEquals(CallMode.RINGING, CallMode.of(AudioManager.MODE_RINGTONE))
        for (mode in listOf(AudioManager.MODE_NORMAL, AudioManager.MODE_CURRENT, AudioManager.MODE_INVALID, 42)) {
            assertEquals("mode $mode", CallMode.NONE, CallMode.of(mode))
        }
    }
}
