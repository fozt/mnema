package dev.mnema.app.ui

import dev.mnema.app.launch.PairingState
import dev.mnema.app.ui.components.clipText
import dev.mnema.app.ui.onboarding.canFinish
import dev.mnema.app.ui.onboarding.onboardingSteps
import dev.mnema.app.ui.settings.manualPort
import dev.mnema.app.ui.settings.pairingSectionState
import dev.mnema.app.ui.settings.parsePort
import dev.mnema.app.ui.settings.serverSummary
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SettingsUiTest {
    @Test
    fun onboardingFinishesOnceRequiredStepsAreDone() {
        val withoutFiles = onboardingSteps(false, true, true, true, true)
        val onlyFiles = onboardingSteps(true, false, false, false, false)

        assertFalse(canFinish(withoutFiles))
        assertTrue(canFinish(onlyFiles))
    }

    @Test
    fun serverRowShowsTheAddressWithoutToken() {
        assertEquals("box:8000", serverSummary("http://box:8000#token=s3cr3"))
        assertEquals("mnema.example.org/base", serverSummary("https://mnema.example.org/base"))
        assertEquals("Не настроен", serverSummary(""))
        assertEquals("Не настроен", serverSummary("не адрес"))
    }

    @Test
    fun pastedSecretLosesSpacesAndLineBreaksAroundIt() {
        assertEquals("http://box:8000#token=s3cr3", clipText("  http://box:8000#token=s3cr3\n"))
        assertEquals("AIzaSy abc", clipText("\tAIzaSy abc \r\n"))
        for (empty in listOf(null, "", " \n ")) assertNull("$empty", clipText(empty))
    }

    @Test
    fun staleOutcomeOfTheServiceIsNotShownNextToPaired() {
        listOf(PairingState.Idle, PairingState.Failed("Время на сопряжение вышло"), PairingState.Paired).forEach { stale ->
            val section = pairingSectionState(paired = true, serviceState = stale)

            assertFalse(stale.toString(), section.shown)
            assertNull(stale.toString(), section.line)
            assertFalse(stale.toString(), section.problem)
        }
    }

    @Test
    fun launchWithoutPairingLeadsThroughPairingAgain() {
        val section = pairingSectionState(paired = true, serviceState = PairingState.Idle, reconnect = true)

        assertTrue(section.shown)
        assertEquals("Подключить заново", section.connect)
        assertTrue(section.primary)
        assertNull(section.line)
    }

    @Test
    fun notPairedSectionGuidesThroughPairing() {
        val idle = pairingSectionState(paired = false, serviceState = PairingState.Idle)

        assertTrue(idle.shown)
        assertEquals("Подключить", idle.connect)
        assertTrue(idle.primary)
        assertNull(idle.line)

        val failed = pairingSectionState(paired = false, serviceState = PairingState.Failed("Проверьте код и повторите"))
        assertEquals("Проверьте код и повторите", failed.line)
        assertTrue(failed.problem)
    }

    @Test
    fun pairingAgainShowsInstructionsAndProgressNextToPaired() {
        val waiting = pairingSectionState(paired = true, serviceState = PairingState.WaitingCode(37123))

        assertTrue(waiting.shown)
        assertEquals("Введите код в уведомлении", waiting.line)
        assertFalse(waiting.problem)
        assertEquals("Подключить заново", waiting.connect)
        assertTrue(pairingSectionState(paired = true, serviceState = PairingState.Searching(windowClosed = true)).problem)
    }

    @Test
    fun manualPortFieldShowsOnlyThePortTheServiceSeesNow() {
        assertEquals("45955", manualPort(PairingState.WaitingCode(45955)))
        assertEquals("", manualPort(PairingState.Searching(windowClosed = true)))
        assertEquals("", manualPort(PairingState.Searching()))
        assertEquals("", manualPort(PairingState.Idle))
        assertEquals("", manualPort(PairingState.Failed("Проверьте код и повторите")))
        assertEquals("", manualPort(PairingState.Paired))
        assertNull(manualPort(PairingState.Pairing))
    }

    @Test
    fun pairingPortIsTakenFromTheWholeAddressToo() {
        assertEquals(37123, parsePort("37123"))
        assertEquals(37123, parsePort(" 192.168.1.5:37123 "))
        assertNull(parsePort("70000"))
        assertNull(parsePort("порт"))
        assertNull(parsePort(""))
    }
}
