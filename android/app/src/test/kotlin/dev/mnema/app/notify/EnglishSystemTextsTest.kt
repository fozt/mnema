package dev.mnema.app.notify

import android.content.pm.PackageInstaller
import dev.mnema.app.i18n.Ui
import dev.mnema.app.i18n.UiLang
import dev.mnema.app.launch.PairingState
import dev.mnema.app.launch.pairingOutcome
import dev.mnema.app.processing.Problems
import dev.mnema.app.settings.ColorSource
import dev.mnema.app.settings.SummaryLang
import dev.mnema.app.settings.ThemeMode
import dev.mnema.app.update.InstallOutcome
import dev.mnema.app.update.installOutcome
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** Notifications, processing problems, settings choices and install outcomes in an English interface. */
class EnglishSystemTextsTest {
    @get:Rule
    val tmp = TemporaryFolder()

    @Before fun english() { Ui.lang = UiLang.EN }

    @After fun russian() { Ui.lang = UiLang.RU }

    private val cyrillic = Regex("[А-Яа-яЁё]")

    private fun assertEnglish(texts: List<String?>) {
        val russian = texts.filterNotNull().filter { cyrillic.containsMatchIn(it) }
        assertEquals(emptyList<String>(), russian)
    }

    private fun NoticeSpec.texts(): List<String?> = listOf(title, text) + buttons.map { it.label }

    @Test fun notificationsAreEnglish() {
        val result = ResultNotice.of(tmp.newFolder("2026-10-21_14-35"))
        val notices = listOf(
            Notices.call(incoming = true),
            Notices.call(incoming = false),
            Notices.callEnded(),
            Notices.processingDone(result, "Daily"),
            Notices.processingFailed(result, Problems.JOB_LOST),
            Notices.startHelper(),
            pairingOutcome(PairingState.Paired)!!,
            pairingOutcome(PairingState.Failed("x"))!!,
        )
        assertEnglish(notices.flatMap { it.texts() })
        assertEquals("Incoming call", Notices.call(incoming = true).title)
        assertEquals(listOf("Record"), Notices.call(incoming = true).buttons.map { it.label })
    }

    @Test fun processingProblemsAreEnglish() {
        assertEnglish(listOf(Problems.NO_NODE, Problems.TOKEN, Problems.NO_SOUND, Problems.NO_TRANSCRIPT, Problems.JOB_LOST,
            Problems.WRITE, Problems.PHONE_FAILURE, Problems.upload("timeout")))
        assertEquals("Mac mini is unavailable", Problems.nodeDown("Mac mini"))
    }

    @Test fun settingsChoicesAreEnglish() {
        assertEnglish(ThemeMode.entries.map { it.title } + ColorSource.entries.map { it.title } + SummaryLang.entries.map { it.title })
        assertEquals("System default", ThemeMode.SYSTEM.title)
    }

    @Test fun installOutcomesAreEnglish() {
        val statuses = listOf(PackageInstaller.STATUS_FAILURE, PackageInstaller.STATUS_FAILURE_CONFLICT, PackageInstaller.STATUS_FAILURE_ABORTED,
            PackageInstaller.STATUS_FAILURE_BLOCKED, PackageInstaller.STATUS_FAILURE_INVALID, PackageInstaller.STATUS_FAILURE_INCOMPATIBLE,
            PackageInstaller.STATUS_FAILURE_STORAGE, PackageInstaller.STATUS_FAILURE_TIMEOUT)
        val texts = statuses.map { (installOutcome(it, null) as InstallOutcome.Failure).text } +
            (installOutcome(PackageInstaller.STATUS_FAILURE, "signatures do not match") as InstallOutcome.Failure).text
        assertEnglish(texts)
        assertFalse(texts.any { it.isBlank() })
    }

    @Test fun theSameTextFollowsTheLanguageOfTheMoment() {
        assertEquals("Transcript ready", Notices.processingDone(ResultNotice.of(tmp.newFolder("a")), "x").title)
        Ui.lang = UiLang.RU
        assertEquals("Расшифровка готова", Notices.processingDone(ResultNotice.of(tmp.newFolder("b")), "x").title)
    }
}
