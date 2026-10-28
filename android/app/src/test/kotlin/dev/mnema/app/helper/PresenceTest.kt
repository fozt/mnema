package dev.mnema.app.helper

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PresenceTest {
    private val zeros = ShortArray(1600)
    private val voice = ShortArray(1600) { if (it % 2 == 0) 40 else -40 }

    @Test
    fun soundMeansNonZeroSamplesInTheLastThreeSeconds() {
        val track = Presence()
        assertFalse("nothing arrived yet", track.sound(0))

        track.add(voice, voice.size, true, 10_000)
        track.add(zeros, zeros.size, true, 12_000)

        assertTrue(track.sound(12_999))
        assertFalse(track.sound(13_000))
        assertEquals("zeros since the last sound", 5_000L, track.silentMs(0, 15_000))
        assertEquals("the call started after the sound", 1_000L, track.silentMs(14_000, 15_000))
    }

    @Test
    fun secondsWithSoundCountOnlyWhatWentIntoTheFile() {
        val track = Presence()
        track.add(voice, voice.size, true, 1_000)
        assertTrue(track.tick())
        track.add(voice, voice.size, false, 2_000)
        assertFalse("the track is muted or paused", track.tick())
        track.add(zeros, zeros.size, true, 3_000)
        assertFalse(track.tick())

        assertEquals(1L, track.seconds())
        assertTrue("sound arrived anyway", track.sound(2_500))
    }
}
