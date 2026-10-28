package dev.mnema.app.ui

import dev.mnema.app.helper.HelperState
import dev.mnema.app.helper.HelperStatus
import dev.mnema.app.launch.LaunchResult
import dev.mnema.app.launch.PairingState
import dev.mnema.app.ui.record.RecordViewModel
import dev.mnema.app.ui.record.launchMessage
import dev.mnema.app.ui.record.reopened
import dev.mnema.app.ui.settings.HelperActions
import dev.mnema.app.ui.settings.helperActions
import dev.mnema.app.ui.settings.launchAfterPairing
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class HelperActionsTest {
    private val idle = HelperStatus(v = 1, state = "idle")
    private val recording = idle.copy(state = "recording", folder = "2026-10-22_19-05")
    private val failedStart = idle.copy(error = "Не удалось начать запись: микрофон недоступен")

    private val states = mapOf(
        "unknown" to HelperState.Unknown,
        "not running" to HelperState.NotRunning,
        "running" to HelperState.Running(idle, outdated = false),
        "outdated" to HelperState.Running(idle, outdated = true),
        "error" to HelperState.Running(failedStart, outdated = false),
        "recording" to HelperState.Running(recording, outdated = false),
        "outdated recording" to HelperState.Running(recording, outdated = true),
        "error recording" to HelperState.Running(recording.copy(error = "Помощник не понял команду. Перезапустите его"), outdated = true),
    )
    private val launches = listOf(
        null,
        LaunchResult.Started,
        LaunchResult.NotPaired,
        LaunchResult.NoWifi,
        LaunchResult.Failed("Помощник не ответил"),
    )
    private val notRefused = launches - LaunchResult.NotPaired
    private val nothing = HelperActions(launch = null, reconnect = false, forget = false)
    private val reconnectOnly = HelperActions(launch = null, reconnect = true, forget = true)

    @Test
    fun withoutPairingThereIsNothingToLaunchOrForget() {
        for ((name, state) in states) {
            for (launch in launches) {
                assertEquals("$name, $launch", nothing, helperActions(state, false, launch))
            }
        }
    }

    @Test
    fun launchIsOfferedOnlyWhenNotRunningOutdatedOrFailed() {
        val expected = mapOf(
            "unknown" to null,
            "not running" to "Запустить",
            "running" to null,
            "outdated" to "Перезапустить",
            "error" to "Перезапустить",
            "recording" to null,
            "outdated recording" to null,
            "error recording" to null,
        )

        for ((name, state) in states) {
            for (launch in notRefused) {
                assertEquals(
                    "$name, $launch",
                    HelperActions(launch = expected.getValue(name), reconnect = false, forget = true),
                    helperActions(state, true, launch),
                )
            }
        }
    }

    @Test
    fun reconnectOnlyAfterLaunchFoundNoPairing() {
        for ((name, state) in states) {
            assertEquals(name, reconnectOnly, helperActions(state, true, LaunchResult.NotPaired))
            for (launch in notRefused) assertEquals("$name, $launch", false, helperActions(state, true, launch).reconnect)
        }
    }

    @Test
    fun newPairingLiftsTheRefusalAndBringsLaunchBack() {
        val kept = launchAfterPairing(LaunchResult.NotPaired, PairingState.Paired)

        assertNull(kept)
        assertEquals(
            HelperActions(launch = "Запустить", reconnect = false, forget = true),
            helperActions(HelperState.NotRunning, true, kept),
        )
    }

    @Test
    fun refusalStaysUntilPairingSucceeds() {
        val services = listOf(
            PairingState.Idle,
            PairingState.Searching(),
            PairingState.Searching(windowClosed = true),
            PairingState.WaitingCode(37_123),
            PairingState.Pairing,
            PairingState.Failed("Проверьте код и повторите"),
            PairingState.Paired,
        )

        for (launch in launches) {
            for (service in services) {
                val lifted = launch == LaunchResult.NotPaired && service == PairingState.Paired
                assertEquals("$launch, $service", if (lifted) null else launch, launchAfterPairing(launch, service))
            }
        }
    }

    @Test
    fun reopenedRecordScreenOffersLaunchAgainAfterRefusal() {
        val refused = RecordViewModel.Launch(
            paired = true,
            message = launchMessage(LaunchResult.NotPaired),
            result = LaunchResult.NotPaired,
        )

        val noWifi = RecordViewModel.Launch(paired = true, message = launchMessage(LaunchResult.NoWifi), result = LaunchResult.NoWifi)
        val sendFailed = RecordViewModel.Launch(
            paired = true,
            message = "Не удалось передать команду помощнику",
            result = LaunchResult.NotPaired,
        )

        val reopened = refused.reopened(paired = true)

        assertEquals(RecordViewModel.Launch(paired = true), reopened)
        assertEquals("Запустить", helperActions(HelperState.NotRunning, reopened.paired, reopened.result).launch)
        assertEquals(noWifi, noWifi.reopened(paired = true))
        assertEquals(noWifi.copy(paired = false), noWifi.reopened(paired = false))
        assertEquals("чужое сообщение остаётся", sendFailed.copy(result = null), sendFailed.reopened(paired = true))
    }
}
