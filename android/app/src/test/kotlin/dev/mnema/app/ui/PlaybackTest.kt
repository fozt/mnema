package dev.mnema.app.ui

import dev.mnema.app.ui.components.SPEEDS
import dev.mnema.app.ui.components.resyncTarget
import dev.mnema.app.ui.components.speedChoice
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PlaybackTest {
    @Test
    fun storedSpeedIsOneOfTheMenu() {
        for (speed in SPEEDS) assertEquals(speed, speedChoice(speed))
        assertEquals(1.25f, speedChoice(1.3f))
        assertEquals(2f, speedChoice(3f))
        assertEquals(1f, speedChoice(0f))
        assertEquals(1f, speedChoice(Float.NaN))
    }

    @Test
    fun driftOverAHundredFiftyMsPullsTheSecondTrackToTheFirst() {
        // second track -> where to pull it, null leaves it as is
        val cases = listOf(
            10_100L to null,
            9_850L to null,
            9_800L to 10_000L,
            10_151L to 10_000L,
        )
        for ((follower, expected) in cases) {
            assertEquals("$follower", expected, resyncTarget(leaderMs = 10_000, followerMs = follower, followerLengthMs = 60_000))
        }
    }

    @Test
    fun shorterTrackThatHasEndedIsNotPulledBack() {
        assertNull(resyncTarget(leaderMs = 45_000, followerMs = 40_000, followerLengthMs = 40_000))
        assertNull(resyncTarget(leaderMs = 45_000, followerMs = 0, followerLengthMs = 0))
    }
}
