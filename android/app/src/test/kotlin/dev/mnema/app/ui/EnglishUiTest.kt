package dev.mnema.app.ui

import dev.mnema.app.helper.HelperState
import dev.mnema.app.helper.HelperStatus
import dev.mnema.app.i18n.Ui
import dev.mnema.app.i18n.UiLang
import dev.mnema.app.launch.PairingState
import dev.mnema.app.recordings.AudioCleanup
import dev.mnema.app.recordings.CleanupRecord
import dev.mnema.app.recordings.ProcessingState
import dev.mnema.app.recordings.RecordingItem
import dev.mnema.app.server.Health
import dev.mnema.app.server.NodeCheck
import dev.mnema.app.sync.SyncStatus
import dev.mnema.app.ui.onboarding.onboardingSteps
import dev.mnema.app.ui.onboarding.stepNote
import dev.mnema.app.ui.record.TrackSound
import dev.mnema.app.ui.record.heroDate
import dev.mnema.app.ui.record.helperLine
import dev.mnema.app.ui.record.recordUi
import dev.mnema.app.ui.recordings.DetailAction
import dev.mnema.app.ui.recordings.PartForm
import dev.mnema.app.ui.recordings.audioAsk
import dev.mnema.app.ui.recordings.cleanupAsk
import dev.mnema.app.ui.recordings.dayLabel
import dev.mnema.app.ui.recordings.recordingAsk
import dev.mnema.app.ui.recordings.recordingLabel
import dev.mnema.app.ui.recordings.recordingSubtitle
import dev.mnema.app.ui.recordings.recordingTitle
import dev.mnema.app.ui.recordings.serverMissingAsk
import dev.mnema.app.ui.recordings.sizeLabel
import dev.mnema.app.ui.recordings.stateLabel
import dev.mnema.app.ui.settings.UpdateAction
import dev.mnema.app.ui.settings.nodeModelSummary
import dev.mnema.app.ui.settings.nodeStatusLabel
import dev.mnema.app.ui.settings.pairingLine
import dev.mnema.app.ui.settings.rowState
import dev.mnema.app.update.UpdateStage
import java.io.File
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Before
import org.junit.Test

/** The main screens render in English when the interface language is English. */
class EnglishUiTest {
    private val before = Ui.lang
    private val zone = ZoneId.of("Europe/Moscow")
    private val today = LocalDate.of(2026, 10, 22)
    private val cyrillic = Regex("[А-Яа-яЁё]")

    @Before
    fun english() {
        Ui.lang = UiLang.EN
    }

    @After
    fun restore() {
        Ui.lang = before
    }

    private fun assertEnglish(texts: Iterable<String?>) {
        val russian = texts.filterNotNull().filter { cyrillic.containsMatchIn(it) }
        assertEquals(emptyList<String>(), russian)
    }

    private fun at(text: String): Long = LocalDateTime.parse(text).atZone(zone).toInstant().toEpochMilli()

    private fun item(state: ProcessingState, title: String = "Daily", timeTitle: Boolean = false, startedAt: Long = at("2026-10-21T09:05:00")) =
        RecordingItem(File("2026-10-21_$title"), title, startedAt, 65.0, "", state, recordingNow = false, timeTitle = timeTitle)

    @Test
    fun recordTabIsEnglish() {
        val idle = HelperStatus(v = 1, state = "idle")
        val recording = HelperStatus(v = 1, state = "recording", folder = "2026-10-22_14-35", startedMs = 1_000L, callRms = 0.0, micRms = 32.0)
        val states = listOf(HelperState.Unknown, HelperState.NotRunning, HelperState.Running(idle, false), HelperState.Running(idle, true),
            HelperState.Running(recording, false), HelperState.Running(recording.copy(state = "paused"), false))
        for (state in states) {
            for (notifications in listOf(true, false)) {
                val ui = recordUi(state, 66_000L, notificationsAllowed = notifications, filesAllowed = notifications)
                assertEnglish(listOf(ui.title, ui.chip, ui.button, ui.warning, ui.pause, ui.cancel, ui.warningDetail, helperLine(state)))
            }
        }
        assertEquals("Ready to record", recordUi(HelperState.Running(idle, false), 0L, notificationsAllowed = true).title)
        assertEnglish(TrackSound.entries.map { it.label } + Tab.entries.map { it.title })
        assertEquals("Thursday, October 22", heroDate(today))
    }

    @Test
    fun recordingsListIsEnglish() {
        assertEquals("Today", dayLabel(today, today))
        assertEquals("Yesterday", dayLabel(today.minusDays(1), today))
        assertEquals("Oct 3", dayLabel(LocalDate.of(2026, 10, 3), today))
        assertEquals("Dec 31, 2025", dayLabel(LocalDate.of(2025, 12, 31), today))
        val recognizing = item(ProcessingState.Recognizing(0.4, ""))
        assertEquals("Yesterday, 9:05 · recognizing · 40%", recordingSubtitle(recognizing, today, zone))
        assertEquals("Daily · Oct 21", recordingLabel(recognizing, today, zone, relative = false))
        assertEquals("Yesterday, 9:05", recordingTitle(item(ProcessingState.Done, "09:05", timeTitle = true), today, zone))
        val states = listOf(ProcessingState.Unprocessed, ProcessingState.WaitingNetwork, ProcessingState.Preparing, ProcessingState.Uploading(0.5),
            ProcessingState.Queued, ProcessingState.Recognizing(null, ""), ProcessingState.Summarizing, ProcessingState.Done, ProcessingState.NoSpeech,
            ProcessingState.NoSummary(""), ProcessingState.AudioOnly(""), ProcessingState.Failed(""))
        assertEnglish(states.map(::stateLabel) + DetailAction.entries.map { it.title })
        assertEnglish(listOf(item(ProcessingState.Done).copy(pendingTitle = "Standup").let { recordingSubtitle(it, today, zone) }))
    }

    @Test
    fun recordingDialogsAreEnglish() {
        assertEquals("1.2 GB", sizeLabel((1.2 * 1024 * 1024 * 1024).toLong()))
        assertEquals("350 MB", sizeLabel(350L * 1024 * 1024))
        val record = CleanupRecord(File("a"), "a", 350L * 1024 * 1024)
        assertEquals("Delete audio of 1 recording?", cleanupAsk(AudioCleanup(listOf(record))).title)
        assertEquals("Delete audio of 2 recordings forever?", cleanupAsk(AudioCleanup(listOf(record, record), trash = false)).title)
        assertEquals("700 MB will be freed. The texts will stay.", cleanupAsk(AudioCleanup(listOf(record, record))).text)
        val asks = listOf(recordingAsk(true, true), recordingAsk(false, false), audioAsk(true), audioAsk(false))
        assertEnglish(asks.flatMap { listOf(it.title, it.text, it.button) })
        val missing = serverMissingAsk(SyncStatus(serverMissing = mutableListOf("a")))
        assertEnglish(listOf(missing.text, missing.delete))
        val part = PartForm(length = 7200.0).typeEnd("0:30").commitEnd()
        assertEquals("The whole recording is selected", PartForm(length = 7200.0).caption)
        assertEquals(true, part.caption.startsWith("Selected "))
    }

    @Test
    fun settingsAreEnglish() {
        val manifest = dev.mnema.app.update.UpdateManifest(version = "0.2.9", versionCode = 209, apk = "https://x/a.apk", sha256 = "c".repeat(64),
            size = 14_680_064, minSdk = 34)
        val noon = today.atTime(12, 0).atZone(zone).toInstant().toEpochMilli()
        assertEquals("Version 0.2.8", rowState("0.2.8", UpdateStage.Idle, noon, today, zone, false).title)
        assertEquals("Checked today", rowState("0.2.8", UpdateStage.Idle, noon, today, zone, false).subtitle)
        assertEquals("Checked Oct 3", rowState("0.2.8", UpdateStage.Idle, at("2026-10-03T12:00:00"), today, zone, false).subtitle)
        assertEquals("0.2.9 available · 14 MB", rowState("0.2.8", UpdateStage.Available(manifest), noon, today, zone, false).subtitle)
        assertEquals("Downloading… 40%", rowState("0.2.8", UpdateStage.Downloading(manifest, 0.4f), noon, today, zone, false).subtitle)
        assertEnglish(UpdateAction.entries.map { it.title })
        val steps = onboardingSteps(files = false, notifications = true, battery = false, paired = false, server = false)
        assertEquals("All files access", steps.first().title)
        assertEnglish(steps.flatMap { listOf(it.title, stepNote(it)) })
        val checks = listOf(null, NodeCheck(Health.Ok, llm = true), NodeCheck(Health.Ok), NodeCheck(Health.TokenRejected), NodeCheck(Health.Unreachable))
        assertEnglish(checks.map(::nodeStatusLabel) + nodeModelSummary(null))
        assertEnglish(listOf(PairingState.Idle, PairingState.Pairing, PairingState.Paired).map(::pairingLine))
        assertFalse(cyrillic.containsMatchIn(nodeModelSummary("gemini/gemini-flash-latest")))
    }
}
