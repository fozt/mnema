package dev.mnema.app.helper

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CadenceTest {
    private val cadence = Cadence()

    @Test
    fun idleStatusIsDueFirstThenOnceInTwoSeconds() {
        assertTrue("before the first recording the status is due at once", cadence.statusDue(false, 10_000))
        cadence.statusWritten(10_000)

        for (tick in 1..6) {
            assertFalse("tick $tick", cadence.statusDue(false, 10_000 + tick * Cadence.TICK_MS))
        }
        assertTrue(cadence.statusDue(false, 10_000 + Cadence.IDLE_STATUS_MS))
    }

    @Test
    fun recordingStatusIsDueEveryTick() {
        cadence.statusWritten(10_000)

        assertTrue(cadence.statusDue(true, 10_000 + Cadence.TICK_MS))
        cadence.statusWritten(10_300)
        assertTrue(cadence.statusDue(true, 10_600))
    }

    @Test
    fun idleStatusAfterRecordingWaitsFromLastWrite() {
        cadence.statusWritten(10_000)
        assertTrue(cadence.statusDue(true, 10_300))
        cadence.statusWritten(10_300)

        assertFalse(cadence.statusDue(false, 10_600))
        assertTrue(cadence.statusDue(false, 12_300))
    }
}
