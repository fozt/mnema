package dev.mnema.app.update

import dev.mnema.app.launch.LaunchResult
import kotlinx.coroutines.CancellationException
import java.io.IOException
import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.delay
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PackageReplacedTest {
    private val timeoutMs = LAUNCH_TIMEOUT_MS

    @Test
    fun launchThatDidNotWorkAsksToStartTheHelper() = runTest {
        assertFalse(relaunchFailed(paired = true, timeoutMs) { LaunchResult.Started })
        assertTrue(relaunchFailed(paired = true, timeoutMs) { LaunchResult.NoWifi })
        assertTrue(relaunchFailed(paired = true, timeoutMs) { LaunchResult.Failed("Помощник не ответил") })
        assertTrue(relaunchFailed(paired = true, timeoutMs) { LaunchResult.NotPaired })
    }

    @Test
    fun launchExceptionNeedsTheSameNoticeAndIsReported() = runTest {
        val cause = IOException("ADB disconnected")
        var logged: Exception? = null
        assertTrue(relaunchFailed(paired = true, timeoutMs, onError = { logged = it }) { throw cause })
        assertTrue(logged is IOException)
        assertEquals(cause.message, logged?.message)
    }

    @Test
    fun cancellationIsNotTurnedIntoAHelperFailureNotice() = runTest {
        val cancellation = CancellationException("app stopping")
        try {
            relaunchFailed(paired = true, timeoutMs, onError = { fail("Cancellation must not be reported") }) { throw cancellation }
            fail("Cancellation must propagate")
        } catch (actual: CancellationException) { assertEquals(cancellation.message, actual.message) }
    }

    @Test
    fun launchStuckPastTheTimeoutAsksToStartTheHelper() = runTest {
        assertTrue(relaunchFailed(paired = true, timeoutMs) { awaitCancellation() })
    }

    @Test
    fun slowLaunchOverWifiIsAwaitedToTheEnd() = runTest {
        // the worst Kadb start: port discovery 5 s, connection 5 s, the helper answer 5 s and the track choice
        assertFalse(
            relaunchFailed(paired = true, timeoutMs) {
                delay(16_500)
                LaunchResult.Started
            },
        )
    }

    @Test
    fun withoutPairingNothingIsLaunchedOrShown() = runTest {
        var launched = false
        assertFalse(
            relaunchFailed(paired = false, timeoutMs) {
                launched = true
                LaunchResult.Started
            },
        )
        assertFalse(launched)
    }
}
