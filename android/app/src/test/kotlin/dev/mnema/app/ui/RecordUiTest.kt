package dev.mnema.app.ui

import dev.mnema.app.helper.HelperCommand
import dev.mnema.app.helper.HelperState
import dev.mnema.app.helper.HelperStatus
import dev.mnema.app.helper.SourceChoice
import dev.mnema.app.recordings.ProcessingState
import dev.mnema.app.recordings.RecordingItem
import dev.mnema.app.ui.record.RecordAction
import dev.mnema.app.ui.record.sourcesOutOfSync
import dev.mnema.app.ui.record.helperLine
import dev.mnema.app.ui.record.lastRecording
import dev.mnema.app.ui.record.level
import dev.mnema.app.ui.record.pauseCommand
import dev.mnema.app.ui.record.recordAction
import dev.mnema.app.ui.record.recordUi
import dev.mnema.app.ui.record.SectionPick
import dev.mnema.app.ui.record.sectionPick
import dev.mnema.app.ui.record.sectionTitle
import java.io.File
import java.time.LocalDateTime
import java.time.ZoneId
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class RecordUiTest {
    private val now = 1_761_046_500_000L
    private val idle = HelperStatus(v = 1, state = "idle")
    private val recording = HelperStatus(
        v = 1,
        state = "recording",
        folder = "2026-10-21_14-35",
        startedMs = now - 65_000,
        callRms = 32768.0,
        micRms = 32.768,
        micSilenced = false,
    )

    private fun running(status: HelperStatus, outdated: Boolean = false) = HelperState.Running(status, outdated)

    @Test
    fun helperNotRunningOffersLaunchInsteadOfRecording() {
        val ui = recordUi(HelperState.NotRunning, now, notificationsAllowed = true)

        assertEquals("Помощник не запущен", ui.title)
        assertEquals("Начать запись", ui.button)
        assertFalse(ui.enabled)
        assertTrue(ui.showLaunch)
        assertEquals("Помощник не запущен", helperLine(HelperState.NotRunning))
        assertNull(ui.timer)
        assertNull(ui.warning)
    }

    @Test
    fun unknownStateWaitsWithoutOfferingLaunch() {
        val ui = recordUi(HelperState.Unknown, now, notificationsAllowed = true)

        assertFalse(ui.enabled)
        assertFalse(ui.showLaunch)
        assertNull(ui.timer)
        assertEquals("Проверяю помощника…", helperLine(HelperState.Unknown))
        assertEquals("Запись", ui.title)
    }

    @Test
    fun idleHelperIsReadyToRecord() {
        val ui = recordUi(running(idle), now, notificationsAllowed = true)

        assertEquals("Готов к записи", ui.title)
        assertEquals("Начать запись", ui.button)
        assertTrue(ui.enabled)
        assertFalse(ui.showLaunch)
        assertEquals("Помощник запущен", helperLine(running(idle)))
        assertNull(ui.timer)
        assertNull(ui.warning)
        assertEquals(0f, ui.callLevel, 0f)
        assertEquals(0f, ui.micLevel, 0f)
    }

    @Test
    fun recordingShowsStopTimerAndLevels() {
        val ui = recordUi(running(recording), now, notificationsAllowed = true)

        assertEquals("Идёт запись", ui.title)
        assertEquals("Остановить запись", ui.button)
        assertTrue(ui.enabled)
        assertEquals("0:01:05", ui.timer)
        assertEquals(1f, ui.callLevel, 0f)
        assertEquals(0f, ui.micLevel, 0f)
        assertNull(ui.warning)
    }

    @Test
    fun timerCountsHoursAndNeverGoesNegative() {
        assertEquals("1:02:05", recordUi(running(recording.copy(startedMs = now - 3_725_000)), now, true).timer)
        assertEquals("0:00:00", recordUi(running(recording.copy(startedMs = now + 5_000)), now, true).timer)
    }

    private val pausable = recording.copy(paused = false)

    @Test
    fun cancelIsOfferedOnlyForARecordingWithItsFolder() {
        assertEquals("Отменить запись", recordUi(running(recording), now, true).cancel)
        assertEquals("и на паузе", "Отменить запись", recordUi(running(pausable.copy(paused = true, heartbeatMs = now)), now, true).cancel)
        assertNull(recordUi(running(idle), now, true).cancel)
        assertNull(recordUi(HelperState.NotRunning, now, true).cancel)
        assertNull("без папки отменять нечего", recordUi(running(recording.copy(folder = "")), now, true).cancel)
    }

    @Test
    fun pausedRecordingStopsTheTimerAndTheLevels() {
        // started 65 s ago, paused for 20 s now, and paused 5 s before that
        val status = pausable.copy(paused = true, heartbeatMs = now, pausedMs = 25_000, micRms = 32768.0)

        val ui = recordUi(running(status), now, true)

        assertEquals("Пауза", ui.title)
        assertTrue(ui.paused)
        assertEquals("Остановить запись", ui.button)
        assertEquals("0:00:40", ui.timer)
        assertEquals(0f, ui.callLevel, 0f)
        assertEquals(0f, ui.micLevel, 0f)
        val later = status.copy(heartbeatMs = now + 30_000, pausedMs = 55_000)
        assertEquals("пауза продолжается — время стоит", "0:00:40", recordUi(running(later), now + 30_400, true).timer)
    }

    @Test
    fun afterResumeTheTimerLeavesOutThePauses() {
        val ui = recordUi(running(pausable.copy(pausedMs = 25_000)), now, true)

        assertEquals("Идёт запись", ui.title)
        assertFalse(ui.paused)
        assertEquals("0:00:40", ui.timer)
    }

    @Test
    fun pauseButtonSendsWhatTheHeartbeatAsksFor() {
        assertNull(pauseCommand(HelperState.NotRunning))
        assertNull(pauseCommand(running(idle.copy(paused = false))))
        assertEquals(HelperCommand.PAUSE, pauseCommand(running(recording)))
        assertEquals(HelperCommand.PAUSE, pauseCommand(running(pausable)))
        assertEquals(HelperCommand.RESUME, pauseCommand(running(pausable.copy(paused = true))))
    }

    @Test
    fun silencedMicrophoneIsWarnedAboveOtherMarks() {
        val status = recording.copy(micSilenced = true)

        val ui = recordUi(running(status), now, notificationsAllowed = false, filesAllowed = false)

        assertEquals("Android заглушил микрофон", ui.warning)
    }

    @Test
    fun outdatedHelperAsksForRestartButStillRecords() {
        val ui = recordUi(running(idle, outdated = true), now, notificationsAllowed = true)

        assertEquals("Помощник устарел, перезапустите", helperLine(running(idle, outdated = true)))
        assertTrue(ui.enabled)
        assertTrue(ui.showLaunch)
    }

    @Test
    fun outdatedHelperIsNotRestartedDuringRecording() {
        val ui = recordUi(running(recording, outdated = true), now, notificationsAllowed = true)

        assertEquals("Остановить запись", ui.button)
        assertTrue(ui.enabled)
        assertFalse(ui.showLaunch)
    }

    @Test
    fun helperErrorIsShownWhenIdle() {
        val status = idle.copy(error = "Не удалось начать запись: нет доступа к микрофону")

        val ui = recordUi(running(status), now, notificationsAllowed = true)

        assertEquals("Не удалось начать запись: нет доступа к микрофону", ui.warning)
    }

    @Test
    fun missingNotificationsAreMarkedPermanently() {
        val running = recordUi(running(idle), now, notificationsAllowed = false)
        val stopped = recordUi(HelperState.NotRunning, now, notificationsAllowed = false)

        assertEquals("Уведомления выключены", running.warning)
        assertEquals("Уведомления выключены", stopped.warning)
    }

    @Test
    fun recordingNeedsAllFilesAccessButStopDoesNot() {
        val idleUi = recordUi(running(idle), now, notificationsAllowed = false, filesAllowed = false)
        val recordingUi = recordUi(running(recording), now, notificationsAllowed = true, filesAllowed = false)

        assertFalse(idleUi.enabled)
        assertEquals("Нет доступа ко всем файлам", idleUi.warning)
        assertNotEquals("Готов к записи", idleUi.title)
        assertTrue(recordingUi.enabled)
        assertEquals("Остановить запись", recordingUi.button)
    }

    @Test
    fun buttonActionFollowsHelperState() {
        assertEquals(RecordAction.NONE, recordAction(HelperState.Unknown))
        assertEquals(RecordAction.NONE, recordAction(HelperState.NotRunning))
        assertEquals(RecordAction.START, recordAction(running(idle)))
        assertEquals(RecordAction.RELAUNCH_AND_START, recordAction(running(idle, outdated = true)))
        assertEquals(RecordAction.STOP, recordAction(running(recording)))
        assertEquals(RecordAction.STOP, recordAction(running(recording, outdated = true)))
    }

    @Test
    fun levelMapsMinusSixtyToMinusTenDecibelsOntoUnitRange() {
        assertEquals(0f, level(null), 0f)
        assertEquals(1f, level(32768.0), 0f)
        assertEquals(0f, level(32.768), 0.0001f)
        assertEquals(0.5f, level(32768.0 * Math.pow(10.0, -35.0 / 20)), 0.001f)
        assertEquals(1f, level(32768.0 * Math.pow(10.0, -10.0 / 20)), 0.001f)
        assertEquals(0f, level(0.0), 0f)
        assertEquals(0f, level(Double.NaN), 0f)
    }

    private val tracked = idle.copy(micOn = true, callOn = true)

    @Test
    fun chipsShowTheChoiceBeforeAndDuringRecording() {
        val choice = SourceChoice(mic = false, call = true)

        for (state in listOf(HelperState.NotRunning, running(tracked), running(recording))) {
            val ui = recordUi(state, now, true, choice = choice)
            assertFalse(state.toString(), ui.micOn)
            assertTrue(state.toString(), ui.callOn)
        }

        // until the choice is read, the chips show what the helper records
        val unread = recordUi(running(idle.copy(micOn = true, callOn = false)), now, true, choice = null)
        assertTrue(unread.micOn)
        assertFalse(unread.callOn)
    }

    @Test
    fun switchedOffSourceHasNoLevel() {
        val loud = recording.copy(callRms = 32768.0, micRms = 32768.0)

        val micOff = recordUi(running(loud), now, true, choice = SourceChoice(mic = false, call = true))
        val callOff = recordUi(running(loud), now, true, choice = SourceChoice(mic = true, call = false))

        assertEquals(1f, micOff.callLevel, 0f)
        assertEquals(0f, micOff.micLevel, 0f)
        assertEquals(0f, callOff.callLevel, 0f)
        assertEquals(1f, callOff.micLevel, 0f)
    }

    @Test
    fun bothSourcesOffIsWarnedButRecordingStillStarts() {
        val off = SourceChoice(mic = false, call = false)

        val before = recordUi(running(tracked), now, true, choice = off)
        val during = recordUi(running(recording), now, true, choice = off)

        assertEquals("Оба источника выключены", before.warning)
        assertTrue(before.enabled)
        assertEquals(RecordAction.START, recordAction(running(tracked)))
        assertEquals("Оба источника выключены", during.warning)
        assertNull(recordUi(running(tracked), now, true, choice = SourceChoice(mic = false)).warning)
    }

    @Test
    fun switchedOffMicrophoneIsNotReportedAsSilencedByAndroid() {
        val silenced = recording.copy(micSilenced = true)

        assertNull(recordUi(running(silenced), now, true, choice = SourceChoice(mic = false)).warning)
        assertEquals("Android заглушил микрофон", recordUi(running(silenced), now, true, choice = SourceChoice()).warning)
    }

    @Test
    fun choiceIsSentOnlyToACurrentHelperThatWritesOtherwise() {
        val micOff = SourceChoice(mic = false)

        assertTrue(sourcesOutOfSync(running(tracked), micOff))
        assertTrue(sourcesOutOfSync(running(recording.copy(micOn = true, callOn = true)), micOff))
        assertFalse(sourcesOutOfSync(running(tracked), SourceChoice()))
        assertFalse("устаревший помощник команд не знает", sourcesOutOfSync(running(tracked, outdated = true), micOff))
        assertFalse(sourcesOutOfSync(HelperState.NotRunning, micOff))
        assertFalse(sourcesOutOfSync(HelperState.Unknown, micOff))
        assertFalse("выбор ещё не прочитан", sourcesOutOfSync(running(tracked), null))
        assertTrue(sourcesOutOfSync(running(idle), micOff))
    }

    private val zone = ZoneId.of("Europe/Moscow")

    private fun at(text: String): Long = LocalDateTime.parse(text).atZone(zone).toInstant().toEpochMilli()

    private fun item(startedAt: Long, recordingNow: Boolean = false) =
        RecordingItem(File("$startedAt"), "Дейли с командой", startedAt, 1925.0, "", ProcessingState.Done, recordingNow)

    @Test
    fun lastRecordingIsTheNewestOneNotBeingRecorded() {
        val older = item(at("2026-10-21T10:00:00"))
        val newer = item(at("2026-10-22T09:00:00"))
        val live = item(at("2026-10-22T11:00:00"), recordingNow = true)

        assertEquals(newer, lastRecording(listOf(older, live, newer)))
        assertNull(lastRecording(listOf(live)))
        assertNull(lastRecording(emptyList()))
    }

    @Test
    fun sectionIsChosenFromTheLibrarySectionsAndAVanishedOneShowsNoSection() {
        val sections = listOf("Личное", "Работа")

        assertEquals(SectionPick(listOf("", "Личное", "Работа"), "Работа"), sectionPick(sections, "Работа"))
        assertEquals(SectionPick(listOf("", "Личное", "Работа"), ""), sectionPick(sections, ""))
        assertEquals("раздел пропал — запись останется в корне", "", sectionPick(sections, "Удалённый")?.selected)
        assertNull("разделов нет — выбирать не из чего", sectionPick(emptyList(), "Работа"))
        assertEquals(listOf("Без раздела", "Работа"), listOf("", "Работа").map(::sectionTitle))
    }
}
