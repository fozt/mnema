package dev.mnema.app.launch

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.io.IOException
import java.net.ConnectException

class PairingPortsTest {
    private var nowMs = 1_000_000L
    private val ports = PairingPorts { nowMs }

    @Test
    fun codeGoesToTheLatestFoundPort() {
        assertNull(ports.found(37759))
        assertNull(ports.found(42913))

        assertEquals(42913, ports.code("123456"))
    }

    @Test
    fun codeBeforeAnyPortWaitsForTheFirstFoundPort() {
        assertNull(ports.code("123456"))
        nowMs += 10_000

        assertEquals("123456", ports.found(38833))
        assertNull(ports.found(45353))
    }

    @Test
    fun earlyCodeIsKeptForThirtySecondsOnly() {
        ports.code("111111")
        nowMs += 30_000
        assertEquals("111111", ports.found(38833))

        ports.refused(38833)
        ports.code("222222")
        nowMs += 30_001
        assertNull(ports.found(45955))
    }

    @Test
    fun refusedPortIsNotUsedAgain() {
        ports.found(38833)

        assertNull(ports.refused(38833))
        assertNull(ports.code("123456"))
        assertEquals("123456", ports.found(45955))
    }

    @Test
    fun refusedOldPortKeepsTheNewerOne() {
        ports.found(38833)
        ports.found(45955)

        assertEquals(45955, ports.refused(38833))
        assertEquals(45955, ports.code("123456"))
    }

    @Test
    fun keyOnDiskClearsTheLastOutcomeButNotPairingInProgress() {
        assertEquals(PairingState.Idle, clearedWhenPaired(PairingState.Failed("Время на сопряжение вышло")))
        assertEquals(PairingState.Idle, clearedWhenPaired(PairingState.Paired))
        assertEquals(PairingState.Idle, clearedWhenPaired(PairingState.Idle))
        val inProgress = listOf(
            PairingState.Searching(),
            PairingState.Searching(windowClosed = true),
            PairingState.WaitingCode(37123),
            PairingState.Pairing,
        )
        inProgress.forEach { assertEquals(it, clearedWhenPaired(it)) }
    }

    @Test
    fun closedPortMeansSearchingAgainOtherErrorsFail() {
        assertEquals(PairingState.Searching(windowClosed = true), afterPairingError(ConnectException("ECONNREFUSED")))
        assertEquals(PairingState.Failed("Проверьте код и повторите"), afterPairingError(IOException("bad code")))
    }
}
