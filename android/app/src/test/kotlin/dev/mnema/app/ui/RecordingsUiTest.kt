package dev.mnema.app.ui

import dev.mnema.app.settings.settingsFor
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.double
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import dev.mnema.app.sync.resource
import dev.mnema.app.processing.fmtClock
import androidx.lifecycle.SavedStateHandle
import dev.mnema.app.model.AUTO_ASR
import dev.mnema.app.model.DialogLine
import dev.mnema.app.model.Lang
import dev.mnema.app.model.Summary
import dev.mnema.app.processing.ProcessState
import dev.mnema.app.processing.ProcessStage
import dev.mnema.app.processing.ProcessStateFile
import dev.mnema.app.processing.ProcessingScheduler
import dev.mnema.app.processing.ReprocessOptions
import dev.mnema.app.recordings.AudioCleanup
import dev.mnema.app.recordings.CleanupRecord
import dev.mnema.app.recordings.FolderRecordingStore
import dev.mnema.app.recordings.ProcessingState
import dev.mnema.app.recordings.RecordingDetail
import dev.mnema.app.recordings.RecordingItem
import dev.mnema.app.recordings.inProgress
import dev.mnema.app.recordings.outcomeKey
import dev.mnema.app.transcript.WavFixture
import dev.mnema.app.transcript.Meta
import dev.mnema.app.server.AsrOption
import dev.mnema.app.ui.recordings.selectedTracks
import dev.mnema.app.ui.recordings.loadAsrChoices
import dev.mnema.app.ui.recordings.withSelected
import kotlinx.coroutines.runBlocking
import java.io.IOException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.flow.flowOf
import dev.mnema.app.ui.components.durationLabel
import dev.mnema.app.ui.recordings.CleanupDialog
import dev.mnema.app.ui.recordings.DetailAction
import dev.mnema.app.ui.recordings.RecordingsViewModel
import dev.mnema.app.ui.recordings.DayGroup
import dev.mnema.app.ui.recordings.DetailState
import dev.mnema.app.ui.recordings.FolderFollower
import dev.mnema.app.ui.recordings.filterRecordings
import dev.mnema.app.ui.recordings.PartChoice
import dev.mnema.app.ui.recordings.PartForm
import dev.mnema.app.ui.recordings.RecordingViewModel
import dev.mnema.app.ui.recordings.currentTitle
import dev.mnema.app.ui.recordings.detailActions
import dev.mnema.app.ui.recordings.groupByDay
import dev.mnema.app.ui.recordings.parseOffset
import dev.mnema.app.ui.recordings.recordingSubtitle
import dev.mnema.app.ui.recordings.sectionTag
import dev.mnema.app.ui.recordings.recordingLabel
import dev.mnema.app.ui.recordings.recordingTitle
import dev.mnema.app.ui.recordings.reprocessOptions
import dev.mnema.app.ui.recordings.opensSheet
import dev.mnema.app.ui.recordings.sheetAction
import dev.mnema.app.ui.recordings.ReprocessDefaults
import dev.mnema.app.ui.recordings.SheetStart
import dev.mnema.app.ui.recordings.sheetStart
import dev.mnema.app.ui.recordings.canDelete
import dev.mnema.app.ui.recordings.ServerMissingAsk
import dev.mnema.app.ui.recordings.audioAsk
import dev.mnema.app.ui.recordings.cleanupAsk
import dev.mnema.app.ui.recordings.recordingAsk
import dev.mnema.app.ui.recordings.serverMissingAsk
import dev.mnema.app.sync.SyncStatus
import dev.mnema.app.ui.recordings.sharedFiles
import dev.mnema.app.ui.recordings.stateLabel
import dev.mnema.app.ui.recordings.whenLabel
import java.io.File
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import java.util.TimeZone
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class RecordingsUiTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val zone = ZoneId.of("Europe/Moscow")
    private val today = LocalDate.of(2026, 10, 21)

    private fun at(text: String): Long = LocalDateTime.parse(text).atZone(zone).toInstant().toEpochMilli()

    private fun item(
        name: String = "2026-10-21_14-35",
        title: String = "14:35",
        startedAt: Long = at("2026-10-21T14:35:00"),
        seconds: Double = 65.0,
        state: ProcessingState = ProcessingState.Done,
        recordingNow: Boolean = false,
        timeTitle: Boolean = title == "14:35",
    ) = RecordingItem(File(name), title, startedAt, seconds, "", state, recordingNow, timeTitle = timeTitle)

    private fun unnamed(start: String) =
        item(name = start.take(16).replace('T', ' ').replace(':', '.'), title = start.substring(11, 16), startedAt = at(start), timeTitle = true)

    private fun named(
        startedAt: Long = at("2026-10-21T09:05:00"),
        state: ProcessingState = ProcessingState.Done,
        seconds: Double = 65.0,
    ) = item(name = "2026-10-21_Дейли с командой", title = "Дейли с командой", startedAt = startedAt, seconds = seconds, state = state)

    private fun detail(
        item: RecordingItem = item(),
        summary: Summary? = null,
        dialog: List<DialogLine> = emptyList(),
        processable: Boolean = true,
        summarizable: Boolean = dialog.isNotEmpty(),
    ) = RecordingDetail(
        item,
        summary,
        dialog,
        tracks = emptyList(),
        remarks = emptyList(),
        processable = processable,
        summarizable = summarizable,
    )

    @Test
    fun jobWaitingOnTheServerIsQueuedAndInProgress() {
        assertEquals("В очереди", stateLabel(ProcessingState.Queued))
        assertTrue(ProcessingState.Queued.inProgress)
    }

    @Test
    fun recognizingPercentStaysWithinBounds() {
        assertEquals("Распознаётся · 0 %", stateLabel(ProcessingState.Recognizing(-0.2, "")))
        assertEquals("Распознаётся · 100 %", stateLabel(ProcessingState.Recognizing(1.5, "")))
        assertEquals("Распознаётся", stateLabel(ProcessingState.Recognizing(Double.NaN, "")))
    }

    @Test
    fun durationLabelUsesMinutesAndHours() {
        assertEquals("0:00", durationLabel(0.0))
        assertEquals("1:05", durationLabel(65.0))
        assertEquals("1:02:05", durationLabel(3725.0))
        assertEquals("0:59", durationLabel(59.9))
        assertEquals("0:00", durationLabel(-3.0))
        assertEquals("0:00", durationLabel(Double.NaN))
    }

    @Test
    fun shareSendsTheRecordingFilesThatExistNamedByFolder() {
        val dir = tmp.newFolder("2026-10-22_Звонок-маме")
        File(dir, "transcript.txt").writeText("mic: Привет\n")
        File(dir, "summary.txt").writeText("Поговорили с мамой.\n")
        File(dir, "mic.wav").writeBytes(ByteArray(64))

        val files = sharedFiles(dir)

        assertEquals(listOf("2026-10-22_Звонок-маме_transcript.txt", "2026-10-22_Звонок-маме_summary.txt"), files.map { it.name })
        assertEquals(listOf("text/plain", "text/plain"), files.map { it.type })
        assertEquals(listOf(File(dir, "transcript.txt"), File(dir, "summary.txt")), files.map { it.source })

        val empty = tmp.newFolder("2026-10-22_08-35")
        File(empty, "transcript.txt").writeText("")
        assertEquals("пустая расшифровка не отправляется", emptyList<String>(), sharedFiles(empty).map { it.name })
        File(empty, "summary.txt").writeText("Итог.\n")
        assertEquals(listOf("2026-10-22_08-35_summary.txt"), sharedFiles(empty).map { it.name })
    }

    @Test
    fun withoutTrashEveryDeletionAsksToDeleteForever() {
        val records = listOf(CleanupRecord(File("a"), "a", 350L * 1024 * 1024))
        for (trash in listOf(true, false)) {
            for (syncing in listOf(false, true)) {
                val ask = recordingAsk(syncing = syncing, trash = trash)
                assertEquals("запись: синхронизация $syncing, корзина $trash", !trash, ask.forever)
                assertEquals("запись: синхронизация $syncing, корзина $trash", syncing, "других устройствах" in ask.text)
            }
            assertEquals("звук: корзина $trash", !trash, audioAsk(trash = trash).forever)
            assertEquals("очистка: корзина $trash", !trash, cleanupAsk(AudioCleanup(records, trash = trash)).forever)
        }
        for ((count, title) in listOf(1 to "Удалить звук 1 записи?", 2 to "Удалить звук 2 записей?", 11 to "Удалить звук 11 записей?",
            21 to "Удалить звук 21 записи?", 111 to "Удалить звук 111 записей?")) {
            assertEquals(title, cleanupAsk(AudioCleanup(List(count) { records[0] }, trash = true)).title)
        }
        assertEquals(ServerMissingAsk("На сервере нет записей: 2. На телефоне они сохранены.", "Удалить на телефоне"),
            serverMissingAsk(SyncStatus(serverMissing = mutableListOf("a", "b"))))
        assertEquals(ServerMissingAsk("Удалено на другом устройстве: 1", "Удалить навсегда"),
            serverMissingAsk(SyncStatus(serverMissing = mutableListOf("a"), withoutTrash = true)))
    }

    @Test
    fun swipeDeletesOnlyWhatTheCardWouldDelete() {
        assertTrue(canDelete(item()))
        assertTrue(canDelete(item(state = ProcessingState.Failed("Сервер недоступен"))))
        assertFalse(canDelete(item(state = ProcessingState.Unprocessed, recordingNow = true)))
        for (busy in listOf(ProcessingState.Preparing, ProcessingState.Uploading(0.1), ProcessingState.Recognizing(0.2, ""), ProcessingState.Summarizing)) {
            assertFalse(busy.toString(), canDelete(item(state = busy)))
            assertFalse(busy.toString(), DetailAction.DELETE in detailActions(detail(item = item(state = busy)), serverSet = true).more)
        }
    }

    @Test
    fun recordingsGroupByDayNewestFirst() {
        val groups = groupByDay(
            listOf(
                item(name = "a", startedAt = at("2026-10-21T14:35:00")),
                item(name = "b", startedAt = at("2026-10-21T09:05:00")),
                item(name = "c", startedAt = at("2026-10-20T23:59:00")),
                item(name = "d", startedAt = at("2026-09-28T10:00:00")),
                item(name = "e", startedAt = at("2025-12-31T10:00:00")),
            ),
            today,
            zone,
        )

        assertEquals(listOf("Сегодня", "Вчера", "28 сент.", "31 дек. 2025"), groups.map { it.label })
        assertEquals(listOf("a", "b"), groups.first().items.map { it.dir.name })
    }

    @Test
    fun unnamedRecordingIsTitledByDayAndTime() {
        assertEquals("Сегодня, 14:35", recordingTitle(item(), today, zone))
        assertEquals("Вчера, 10:05", recordingTitle(unnamed("2026-10-20T10:05:00"), today, zone))
        assertEquals("19 окт., 9:05", recordingTitle(unnamed("2026-10-19T09:05:00"), today, zone))
        assertEquals("31 дек. 2025, 23:59", recordingTitle(unnamed("2025-12-31T23:59:00"), today, zone))
        assertEquals("Сегодня, 0:00", recordingTitle(unnamed("2026-10-21T00:00:00"), today, zone))
        assertEquals("Дейли с командой", recordingTitle(named(), today, zone))
    }

    @Test
    fun subtitleIsTheStateAfterDayAndTimeOfANamedRecording() {
        assertEquals("готово", recordingSubtitle(item(), today, zone))
        assertEquals("распознаётся · 40 %", recordingSubtitle(item(state = ProcessingState.Recognizing(0.4, "")), today, zone))
        assertEquals("распознаётся", recordingSubtitle(item(state = ProcessingState.Recognizing(null, "")), today, zone))
        assertEquals("отправляется · 60 %", recordingSubtitle(item(state = ProcessingState.Uploading(0.6)), today, zone))
        assertEquals("не расшифрована", recordingSubtitle(item(state = ProcessingState.AudioOnly("Сервер не задан")), today, zone))
        assertEquals("ждёт сети", recordingSubtitle(item(state = ProcessingState.WaitingNetwork), today, zone))
        assertEquals("ошибка", recordingSubtitle(item(state = ProcessingState.Failed("Сервер недоступен")), today, zone))
        assertEquals("идёт запись", recordingSubtitle(item(state = ProcessingState.Unprocessed, recordingNow = true), today, zone))
        assertEquals("Сегодня, 9:05 · готово", recordingSubtitle(named(), today, zone))
        assertEquals(
            "Вчера, 9:05 · распознаётся · 40 %",
            recordingSubtitle(named(at("2026-10-20T09:05:00"), ProcessingState.Recognizing(0.4, "")), today, zone),
        )
        // Midnight is a recording time like any other, it is not hidden.
        assertEquals("28 сент., 0:00 · без пересказа", recordingSubtitle(named(at("2026-09-28T00:00:00"), ProcessingState.NoSummary("")), today, zone))
    }

    @Test
    fun allRecordingsShowTheSectionFirstAndAFilteredListDoesNot() {
        val work = named().copy(section = "Работа")
        val root = named()

        assertEquals("Работа", sectionTag(work, filter = null))
        assertNull("запись без раздела — без тега", sectionTag(root, filter = null))
        assertNull("выбран раздел — у всех строк он один", sectionTag(work, filter = "Работа"))
        assertNull("«Без раздела»", sectionTag(root, filter = ""))
        assertEquals("Работа · Сегодня, 9:05 · готово", recordingSubtitle(work, today, zone, sectionTag(work, null)))
        assertEquals("Сегодня, 9:05 · готово", recordingSubtitle(work, today, zone, sectionTag(work, "Работа")))
        assertEquals("Сегодня, 9:05 · готово", recordingSubtitle(root, today, zone, sectionTag(root, null)))
        val unnamed = item().copy(section = "Работа")
        assertEquals("Работа · готово", recordingSubtitle(unnamed, today, zone, sectionTag(unnamed, null)))
    }

    @Test
    fun detailHeaderNamesDayAndTimeOnlyWhenTheTitleDoesNot() {
        assertEquals("1:05", whenLabel(item(), today, zone))
        assertEquals("Сегодня, 9:05 · 1:05", whenLabel(named(), today, zone))
        assertEquals("28 сент., 0:00", whenLabel(named(at("2026-09-28T00:00:00"), seconds = 0.0), today, zone))
    }

    @Test
    fun recordingNowOffersOnlyARenameAndAMoveThatSayWhyTheyAreUnavailable() {
        val recording = detailActions(detail(item = item(state = ProcessingState.Unprocessed, recordingNow = true)), serverSet = true)

        assertNull(recording.lead)
        assertEquals(listOf(DetailAction.RENAME, DetailAction.MOVE), recording.more)
        assertEquals("Переименовать можно после записи", recording.blocked(DetailAction.RENAME))
        assertEquals("Переместить можно после записи", recording.blocked(DetailAction.MOVE))
        assertEquals(DetailAction.RENAME, recording.naming)
        assertNull(recording.blocked(DetailAction.DELETE))
        assertFalse(recording.share)
    }

    @Test
    fun processingRecordingOffersStopInsteadOfAnotherProcessing() {
        val busy = listOf(
            ProcessingState.Preparing,
            ProcessingState.WaitingNetwork,
            ProcessingState.Uploading(0.3),
            ProcessingState.Recognizing(0.4, ""),
            ProcessingState.Summarizing,
        ).map { item(state = it) } + item(state = ProcessingState.Summarizing, seconds = 0.0)
        for (recording in busy) {
            val label = "${recording.state}, ${recording.durationS} с"
            val actions = detailActions(detail(item = recording, dialog = listOf(DialogLine("mic", "да"))), serverSet = true)
            assertEquals(label, DetailAction.STOP, actions.lead)
            // the title is set, processing names the folder at the end
            assertEquals(label, listOf(DetailAction.TITLE, DetailAction.MOVE), actions.more)
            assertEquals(label, "Переместить можно после обработки", actions.blocked(DetailAction.MOVE))
            assertEquals(label, DetailAction.TITLE, actions.naming)
            assertNull(label, actions.renameBlocked)
            assertNull(label, actions.blocked(DetailAction.TITLE))
        }
    }

    @Test
    fun leadActionFollowsTheState() {
        val dialog = listOf(DialogLine("mic", "да"))
        fun lead(state: ProcessingState, summary: Summary? = null) =
            detailActions(detail(item = item(state = state), summary = summary, dialog = dialog), serverSet = true).lead

        assertEquals(DetailAction.PROCESS, lead(ProcessingState.Unprocessed))
        assertEquals(DetailAction.RETRY, lead(ProcessingState.Failed("Сервер отказал")))
        assertEquals(DetailAction.SUMMARIZE, lead(ProcessingState.NoSummary("Модель не задана")))
        assertEquals(DetailAction.REPROCESS, lead(ProcessingState.Done, Summary("", listOf("один"))))
    }

    @Test
    fun audioOnlyRecordingIsProcessedNotProcessedAgain() {
        val audio = detail(item = item(state = ProcessingState.AudioOnly("Сервер не задан")))

        for (serverSet in listOf(true, false)) {
            val actions = detailActions(audio, serverSet = serverSet)
            assertEquals(DetailAction.PROCESS, actions.lead)
            assertEquals(listOf(DetailAction.RENAME, DetailAction.MOVE, DetailAction.DELETE), actions.more)
            assertNull(actions.renameBlocked)
            assertNull(actions.blocked(DetailAction.MOVE))
        }
    }

    @Test
    fun everyManualProcessingOpensOneSheetExceptSummaryFromReadyText() {
        val fresh = detail(item = item(state = ProcessingState.Unprocessed))
        val failed = detail(item = item(state = ProcessingState.Failed("Mac mini недоступен")))
        val summaryFailed = detail(item = item(state = ProcessingState.Failed("Лимит модели")), dialog = listOf(DialogLine("mic", "да")))
            .copy(retrySummaryOnly = true)
        assertTrue(opensSheet(DetailAction.PROCESS, fresh))
        assertTrue(opensSheet(DetailAction.REPROCESS, detail()))
        assertTrue(opensSheet(DetailAction.RETRY, failed))
        assertFalse(opensSheet(DetailAction.RETRY, summaryFailed))
        assertFalse(opensSheet(DetailAction.SUMMARIZE, summaryFailed))
        assertEquals(DetailAction.RETRY, detailActions(failed, serverSet = true).lead)
        assertFalse("полный повтор и есть окно", DetailAction.PROCESS in detailActions(failed, serverSet = true).more)
        assertTrue(DetailAction.REPROCESS in detailActions(summaryFailed, serverSet = true).more)
    }

    @Test
    fun retrySheetOpensWithTheFailedRunAndFallsBackToDefaults() {
        val defaults = ReprocessDefaults(Lang.RU, AUTO_ASR)
        val run = ReprocessOptions(Lang.EN, "groq/whisper-large-v3", "Планёрка", 600.0, 5700.0, tracks = setOf("mic"))
        val failed = detail(item = item(state = ProcessingState.Failed("Mac mini недоступен")))
            .copy(tracks = listOf(File("mic.wav"), File("system.wav")), retryContinues = true, failedRun = run)

        val start = sheetStart(failed, defaults, retrying = true)

        assertEquals(SheetStart(Lang.EN, "groq/whisper-large-v3", "Планёрка", setOf("mic")), start)
        assertEquals("без изменений — тот же выбор, что у запуска", ReprocessOptions(Lang.EN, "groq/whisper-large-v3", "Планёрка", tracks = setOf("mic")),
            reprocessOptions(start.lang, start.asr, start.title, currentTitle(failed), PartChoice.Whole, start.tracks))
        val cleared = failed.copy(item = named(state = ProcessingState.Failed("Сбой")), failedRun = ReprocessOptions(autoName = true, tracks = setOf("tape")))
        assertEquals(SheetStart(Lang.RU, AUTO_ASR, ""), sheetStart(cleared, defaults, retrying = true))
        assertEquals(SheetStart(Lang.RU, AUTO_ASR, ""), sheetStart(failed, defaults, retrying = false))
        assertEquals(SheetStart(Lang.RU, AUTO_ASR, "Дейли с командой"),
            sheetStart(failed.copy(item = named(state = ProcessingState.Failed("Сбой")), failedRun = null), defaults, retrying = true))
    }

    @Test
    fun sheetIsProcessUntilTheRecordingHasText() {
        val dialog = listOf(DialogLine("mic", "да"))
        assertEquals("Обработать", sheetAction(detail(item = item(state = ProcessingState.Unprocessed))).title)
        assertEquals("Обработать", sheetAction(detail(item = item(state = ProcessingState.AudioOnly("")))).title)
        assertEquals("Обработать", sheetAction(detail(item = item(state = ProcessingState.Failed("Сбой")))).title)
        assertEquals("Обработать заново", sheetAction(detail(item = item(state = ProcessingState.Failed("Сбой")), dialog = dialog)).title)
        assertEquals("Обработать заново", sheetAction(detail(item = item(state = ProcessingState.AudioOnly("Сохранён только звук")), dialog = dialog)).title)
        assertEquals("Обработать заново", sheetAction(detail(item = item(state = ProcessingState.AudioOnly("")),
            summary = Summary("Обсудили план", listOf("План")))).title)
        assertEquals("Обработать заново", sheetAction(detail(item = item(state = ProcessingState.NoSummary("")), dialog = dialog)).title)
        assertEquals("Обработать заново", sheetAction(detail()).title)
    }

    @Test
    fun moreHoldsWhatIsNotLead() {
        val dialog = listOf(DialogLine("mic", "да"))
        val noSummary = detailActions(detail(item = item(state = ProcessingState.NoSummary("")), dialog = dialog), serverSet = false)
        assertTrue(noSummary.share)
        assertEquals(listOf(DetailAction.REPROCESS, DetailAction.RENAME, DetailAction.MOVE, DetailAction.DELETE), noSummary.more)

        val done = detailActions(detail(summary = Summary("", listOf("один")), dialog = dialog), serverSet = false)
        assertTrue(done.share)
        assertEquals(listOf(DetailAction.RENAME, DetailAction.MOVE, DetailAction.DELETE), done.more)

        val fresh = detailActions(detail(item = item(state = ProcessingState.Unprocessed)), serverSet = false)
        assertEquals(DetailAction.PROCESS, fresh.lead)
        assertEquals(listOf(DetailAction.RENAME, DetailAction.MOVE, DetailAction.DELETE), fresh.more)
        assertFalse(fresh.share)
    }

    @Test
    fun recordingWithoutSoundIsNotProcessedAgain() {
        val silent = detailActions(detail(item = item(seconds = 0.0, state = ProcessingState.Failed("Нет звука"))), serverSet = true)

        assertNull(silent.lead)
        assertEquals(listOf(DetailAction.RENAME, DetailAction.MOVE, DetailAction.DELETE), silent.more)
    }

    @Test
    fun buttonsFollowWhatTheSchedulerAccepts() {
        val dialog = listOf(DialogLine("mic", "да"))
        val textOnly = detailActions(
            detail(item = item(seconds = 0.0, state = ProcessingState.NoSummary("")), dialog = dialog, processable = false),
            serverSet = true,
        )
        assertEquals(DetailAction.SUMMARIZE, textOnly.lead)
        assertEquals(listOf(DetailAction.RENAME, DetailAction.MOVE, DetailAction.DELETE), textOnly.more)

        val foreignWav = detailActions(detail(item = item(state = ProcessingState.Unprocessed), processable = false), serverSet = true)
        assertNull(foreignWav.lead)

        val unreadable = detailActions(
            detail(item = item(state = ProcessingState.NoSummary("")), dialog = dialog, summarizable = false),
            serverSet = true,
        )
        assertEquals(DetailAction.REPROCESS, unreadable.lead)
        assertEquals(listOf(DetailAction.RENAME, DetailAction.MOVE, DetailAction.DELETE), unreadable.more)
    }

    @Test
    fun typedTimeClockAndWholeRecordingFollowTheSharedTable() {
        val table = Json.parseToJsonElement(resource("offsets-v1.json")).jsonObject
        for (row in table.getValue("typed").jsonArray.map { it.jsonObject }) {
            val text = row.getValue("text").jsonPrimitive.content
            assertEquals("«$text»", row.getValue("seconds").jsonPrimitive.doubleOrNull, parseOffset(text))
        }
        for (row in table.getValue("clock").jsonArray.map { it.jsonObject }) {
            assertEquals(row.getValue("clock").jsonPrimitive.content, fmtClock(row.getValue("seconds").jsonPrimitive.double))
        }
        for (row in table.getValue("whole").jsonArray.map { it.jsonObject }) {
            fun value(key: String) = row.getValue(key).jsonPrimitive.double
            val form = PartForm(value("length"), start = value("start"), end = value("end"))
            assertEquals(row.toString(), row.getValue("whole").jsonPrimitive.boolean, form.whole)
        }
    }

    @Test
    fun partFieldsShowFullClockAndFollowTheSlider() {
        val form = PartForm(length = 95.8)
        assertEquals("0:00:00", form.startText)
        assertEquals("0:01:35", form.endText)
        assertEquals("Выбрана вся запись", form.caption)

        val moved = form.slide(10.4, 95.8)
        assertEquals("0:00:10", moved.startText)
        assertEquals("0:01:35", moved.endText)
        assertEquals("Выбрано 0:01:25", moved.caption)
        assertEquals(PartChoice.Part(10.0, 95.0), moved.choice())
    }

    @Test
    fun typedTimeMovesTheSliderOnlyWhenItIsATime() {
        val typed = PartForm(length = 600.0).typeStart("0:01:00")
        assertEquals(60.0, typed.start, 0.0)
        assertEquals("Выбрано 0:09:00", typed.caption)

        val partial = typed.typeStart("0:")
        assertEquals("0:", partial.startText)
        assertEquals(60.0, partial.start, 0.0)
    }

    @Test
    fun typingKeepsTheOtherEdgeUntilTheFieldIsLeft() {
        val form = PartForm(length = 600.0).typeEnd("0:05:00").commitEnd()

        val typing = form.typeStart("0:06:00")
        assertEquals(0.0, typing.start, 0.0)
        assertEquals(300.0, typing.end, 0.0)

        val left = typing.commitStart()
        assertEquals(360.0, left.start, 0.0)
        assertEquals(360.0, left.end, 0.0)
        assertEquals("0:06:00", left.endText)

        val shorter = PartForm(length = 600.0).typeStart("0:04:00").commitStart().typeEnd("0:03:00").commitEnd()
        assertEquals(180.0, shorter.start, 0.0)
        assertEquals("0:03:00", shorter.startText)
    }

    @Test
    fun committedFieldIsWrittenAsFullClock() {
        val form = PartForm(length = 7200.0).typeStart("1ч 5м").commitStart()

        assertEquals("1:05:00", form.startText)
        assertEquals(3900.0, form.start, 0.0)
        // not a time: the field stays as typed, the slider stays put
        val wrong = form.typeStart("пять").commitStart()
        assertEquals("пять", wrong.startText)
        assertEquals(3900.0, wrong.start, 0.0)
    }

    @Test
    fun partMustStartBeforeItsEndWithinTheRecording() {
        fun choice(start: String, end: String) = PartForm(length = 95.8).typeStart(start).typeEnd(end).choice()

        assertEquals(PartChoice.Part(10.0, 95.0), choice("0:00:10", "0:01:35"))
        // the end is not past the recording
        assertEquals(PartChoice.Part(10.0, 95.8), choice("0:00:10", "2:00:00"))
        assertEquals(PartChoice.Whole, choice("0:00:00", "0:01:35"))
        assertEquals(PartChoice.Whole, choice("", ""))
        assertEquals(PartChoice.Invalid("Конец раньше начала"), choice("0:01:00", "0:00:30"))
        assertEquals(PartChoice.Invalid("Конец раньше начала"), choice("0:00:30", "0:00:30"))
        assertEquals(PartChoice.Invalid("Начало 0:02:00 позже конца записи"), choice("0:02:00", ""))
        assertEquals(PartChoice.Invalid("Не понял время «пять»"), choice("пять", "0:01:00"))
        assertEquals(PartChoice.Invalid("Не понял время «0:61»"), choice("0:00:10", "0:61"))
    }

    @Test
    fun optionsCarryChoiceAndOnlyAChangedTitle() {
        val part = PartChoice.Part(10.0, 95.0)

        assertEquals(
            ReprocessOptions(Lang.EN, "whisper", null, 10.0, 95.0),
            reprocessOptions(Lang.EN, "whisper", " Дейли ", "Дейли", part),
        )
        assertEquals(
            ReprocessOptions(Lang.RU, AUTO_ASR, "Ретро", null, null),
            reprocessOptions(Lang.RU, AUTO_ASR, " Ретро ", "Дейли", PartChoice.Whole),
        )
        assertNull(reprocessOptions(Lang.AUTO, AUTO_ASR, "", "", PartChoice.Invalid("Конец раньше начала")))
        // an empty title means naming by the content, even if there was a title
        assertEquals(
            ReprocessOptions(Lang.AUTO, AUTO_ASR, autoName = true),
            reprocessOptions(Lang.AUTO, AUTO_ASR, "  ", "", PartChoice.Whole),
        )
        assertEquals(
            ReprocessOptions(Lang.RU, "gigaam", null, 10.0, 95.0, autoName = true),
            reprocessOptions(Lang.RU, "gigaam", "", "Дейли", part),
        )
    }

    @Test
    fun selectedSourcesCannotBecomeEmptyAndReachOptionsWithTheNodeAsrId() {
        val mic = selectedTracks(setOf("system", "mic"), "system")
        assertEquals(setOf("mic"), mic)
        assertEquals(mic, selectedTracks(mic, "mic"))
        assertEquals(setOf("mic", "system"), selectedTracks(mic, "system"))
        val options = reprocessOptions(Lang.RU, "node-asr-new", "Дейли", "Дейли", PartChoice.Part(10.0, 95.0), mic)!!
        assertEquals(mic, options.tracks)
        assertEquals("node-asr-new", options.asr)
        assertEquals(10.0, options.startS)
        assertEquals(95.0, options.endS)
        assertNull(reprocessOptions(Lang.RU, "auto", "", "", PartChoice.Whole, emptySet()))
    }

    @Test
    fun labelOutsideTheDayHeadingCarriesTheDay() {
        val daily = item(name = "2026-10-20_Дейли", title = "Дейли", startedAt = at("2026-10-20T09:05:00"))
        assertEquals("Дейли · Вчера", recordingLabel(daily, today, zone))
        assertEquals("Дейли · 20 окт.", recordingLabel(daily, today, zone, relative = false))
        val part = item(startedAt = at("2026-10-21T14:35:00")).copy(partLabel = "0.10–1.35")
        assertEquals("Сегодня, 14:35 (0.10–1.35)", recordingLabel(part, today, zone))
        assertEquals("21 окт., 14:35 (0.10–1.35)", recordingLabel(part, today, zone, relative = false))
        assertEquals("Дейли · 20 окт. 2026", recordingLabel(daily, today.plusYears(1), zone, relative = false))
    }

    @Test
    fun aClockLikeManualTitleDoesNotBecomeAnUnnamedRecording() {
        val literal = item(title = "14:35", timeTitle = false)
        assertEquals("14:35", recordingTitle(literal, today, zone))
        assertEquals("14:35", currentTitle(detail(item = literal)))
    }

    @OptIn(ExperimentalCoroutinesApi::class)
    @Test
    fun summaryRetryUsesTextOnlyAndAsrChoicesUseTheSelectedNode() {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try {
            runTest {
                val root = tmp.newFolder("Mnema")
                val dir = File(root, "Моё название").apply { mkdirs() }
                File(dir, "transcript.txt").writeText("mic: Готовая расшифровка")
                File(dir, "summary.txt").writeText("Старая суть.\n\n- Старый пункт")
                Meta.ensureRecording(dir, "id") { null }
                ProcessStateFile.write(dir, ProcessState("id", stage = ProcessStage.FAILED,
                    summaryOnly = true, problem = "Лимит модели", since = outcomeKey(Meta.read(dir))))
                val request = CompletableDeferred<Pair<File, Boolean>>()
                val scheduler = object : ProcessingScheduler {
                    override fun enqueue(dir: File, summaryOnly: Boolean): Boolean {
                        request.complete(dir to summaryOnly)
                        return true
                    }
                    override fun reprocess(dir: File, options: ReprocessOptions): File? = error("Повтор не меняет параметры")
                    override fun cancel(dir: File) = Unit
                }
                val settings = settingsFor("http://chosen-node:9000")
                var offline = false
                val choices = listOf(AsrOption("node-asr-new", "Новый движок узла"))
                val store = FolderRecordingStore(root, backgroundScope, { null }, { false }, Dispatchers.Unconfined)
                val model = RecordingViewModel(store, scheduler, root,
                    SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)), flowOf(settings),
                    loadAsr = { current ->
                        assertEquals(settings.connection, current.connection)
                        if (offline) throw IOException("нет ответа")
                        choices
                    }, io = Dispatchers.Unconfined)
                backgroundScope.launch { model.state.collect {} }
                store.refresh()
                val shown = model.state.first { it is DetailState.Shown } as DetailState.Shown
                assertEquals(DetailAction.RETRY, shown.actions.lead)
                assertFalse(DetailAction.SUMMARIZE in shown.actions.more)
                assertEquals("Моё название", currentTitle(shown.detail))
                assertEquals(choices, model.asrChoices())
                offline = true
                // the node did not answer: the phone does not know the node's engines, so Automatic and the saved choice
                assertEquals(listOf(AsrOption("auto", "Автоматически")), model.asrChoices())
                assertEquals(listOf(AsrOption("auto", "Автоматически"), AsrOption("node-asr-new", "node-asr-new")),
                    model.asrChoices("node-asr-new"))
                model.retry()
                assertEquals(dir to true, request.await())
                assertEquals("Моё название", dir.name)
                assertEquals("Старая суть.\n\n- Старый пункт", File(dir, "summary.txt").readText())
                assertEquals("mic: Готовая расшифровка", File(dir, "transcript.txt").readText())
            }
        } finally { Dispatchers.resetMain() }
    }

    @OptIn(ExperimentalCoroutinesApi::class)
    @Test
    fun theReprocessSheetStartsWithEveryTrackBecauseTheServerDecidesSilence() {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try {
            runTest {
                val root = tmp.newFolder("Mnema")
                val dir = File(root, "Запись").apply { mkdirs() }
                WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(1.0, listOf(0.1 to 0.9)))
                WavFixture.write(File(dir, "system.wav"), WavFixture.samples(1.0))
                val scheduler = object : ProcessingScheduler {
                    override fun enqueue(dir: File, summaryOnly: Boolean) = true
                    override fun reprocess(dir: File, options: ReprocessOptions): File? = dir
                    override fun cancel(dir: File) = Unit
                }
                val store = FolderRecordingStore(root, backgroundScope, { null }, { false }, Dispatchers.Unconfined)
                val model = RecordingViewModel(store, scheduler, root, SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)), io = Dispatchers.Unconfined)
                backgroundScope.launch { model.state.collect {} }
                store.refresh()
                model.state.first { it is DetailState.Shown }
                assertEquals(setOf("mic", "system"), model.soundTracks())
            }
        } finally { Dispatchers.resetMain() }
    }

    @Test
    fun fullRetryAndSummaryFromExistingTranscriptRemainSeparateActions() {
        val recording = detail(item = item(state = ProcessingState.Failed("Узел недоступен")),
            dialog = listOf(DialogLine("mic", "Старый текст")))
        val actions = detailActions(recording, serverSet = true)
        assertEquals(DetailAction.RETRY, actions.lead)
        assertTrue(DetailAction.SUMMARIZE in actions.more)
    }

    @Test
    fun unavailableNodeOffersRetryAndChoosingANodeNextToTheProblem() {
        val down = detail(item = item(state = ProcessingState.Failed("Mac mini недоступен"))).copy(nodeDown = true)
        val actions = detailActions(down, serverSet = true)
        assertEquals(DetailAction.RETRY, actions.lead)
        assertEquals(DetailAction.CHOOSE_NODE, actions.problem)
        assertFalse(DetailAction.CHOOSE_NODE in actions.more)
        assertEquals("Выбрать сервер", DetailAction.CHOOSE_NODE.title)
        assertNull(detailActions(detail(item = item(state = ProcessingState.Failed("Лимит модели"))), serverSet = true).problem)
        assertNull(detailActions(down.copy(item = down.item.copy(state = ProcessingState.Preparing)), serverSet = true).problem)
    }

    @Test
    fun asrChoicesComeFromTheNodeAndTheSavedChoiceIsNotLost() = runBlocking {
        val node = listOf(AsrOption("auto", "Автоматически"), AsrOption("groq/whisper-large-v3", "Groq · whisper-large-v3"))
        assertEquals(node, loadAsrChoices("auto") { node })
        val auto = AsrOption("auto", "Автоматически")
        assertEquals(listOf(auto, AsrOption("groq/whisper-large-v3", "groq/whisper-large-v3")),
            loadAsrChoices("groq/whisper-large-v3") { throw IOException("нет ответа") })
        assertEquals(listOf(auto), loadAsrChoices("auto") { emptyList() })
        val withoutSaved = withSelected(listOf(AsrOption("auto", "Автоматически")), "groq/whisper-large-v3")
        assertEquals(AsrOption("groq/whisper-large-v3", "groq/whisper-large-v3"), withoutSaved.last())
        assertEquals(node, withSelected(node, "groq/whisper-large-v3"))
    }

    @OptIn(ExperimentalCoroutinesApi::class)
    @Test
    fun theListIsEmptyOnlyAfterTheFirstScanFoundNothing() {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try {
            runTest {
                val root = tmp.newFolder("Mnema")
                val store = FolderRecordingStore(root, backgroundScope, { null }, { false }, Dispatchers.Unconfined)
                val model = RecordingsViewModel(store, zone, compute = Dispatchers.Unconfined)
                backgroundScope.launch { model.groups.collect {} }
                runCurrent()
                // not scanned yet: the screen shows no "No recordings yet"
                assertNull(model.groups.value)

                store.refresh()
                assertEquals(emptyList<DayGroup>(), model.groups.first { it != null })

                WavFixture.write(File(File(root, "2026-10-21_14-35").apply { mkdirs() }, "mic.wav"), WavFixture.samples(1.0))
                store.refresh()
                assertEquals(listOf("2026-10-21_14-35"), model.groups.first { !it.isNullOrEmpty() }!!.flatMap { it.items }.map { it.dir.name })
            }
        } finally { Dispatchers.resetMain() }
    }

    @OptIn(ExperimentalCoroutinesApi::class)
    @Test
    fun cleanupIsConfirmedFirstAndThenSaysHowManyRecordingsLostTheirSound() {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try {
            runTest {
                val root = tmp.newFolder("Mnema")
                val dir = File(root, "2026-10-21_10-00").apply { mkdirs() }
                WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(1.0))
                File(dir, "transcript.txt").writeText("mic: текст")
                Meta.ensureRecording(dir, "11111111-1111-1111-1111-111111111111") { Meta.isoNow(at("2026-10-21T10:00:00"), TimeZone.getTimeZone(zone)) }
                Meta.appendRun(dir, JsonObject(mapOf("status" to JsonPrimitive("ok"))))
                val trashed = mutableListOf<String>()
                var onServer = false
                val store = FolderRecordingStore(root, backgroundScope, { null }, { false }, Dispatchers.Unconfined,
                    trash = { trashed += it.name; it.delete() }, textsOnServer = { { onServer } })
                val model = RecordingsViewModel(store, zone)

                model.askCleanup()
                assertEquals(CleanupDialog.Empty(onServer = true), model.cleanup.first { it != null })
                model.dismissCleanup()
                onServer = true
                model.askCleanup()
                val asked = model.cleanup.first { it != null } as CleanupDialog.Confirm
                assertEquals(1, asked.cleanup.records.size)
                assertTrue(trashed.isEmpty())
                model.confirmCleanup()

                assertEquals("Звук удалён у записей: 1", model.notice.first { it != null })
                assertEquals(listOf("mic.wav"), trashed)
                assertTrue(File(dir, "transcript.txt").isFile)
            }
        } finally { Dispatchers.resetMain() }
    }

    @Test
    fun sheetTitleStartsFromTheRecordingOwnName() {
        assertEquals("Дейли", currentTitle(detail(item = item(name = "2026-10-21_Дейли", title = "Дейли"))))
        assertEquals("", currentTitle(detail(item = item(name = "2026-10-21_14-35", title = "14:35"))))
        assertEquals("", currentTitle(detail(item = item(name = "2026-10-21_14-35 (0.10–1.35)", title = "14:35 (0.10–1.35)", timeTitle = true))))
        assertEquals("Дейли", currentTitle(detail(item = item(name = "2026-10-21_Дейли (0.10–1.35)", title = "Дейли"))))
        assertEquals("Планёрка", currentTitle(detail(item = item(name = "2026-10-21_14-35", title = "14:35", timeTitle = true).copy(pendingTitle = "Планёрка"))))
    }

    @OptIn(ExperimentalCoroutinesApi::class)
    @Test
    fun reprocessedPartOpensItsFolderAndRefusalIsExplained() {
        // Main is swapped around runTest: background collectors stop before the reset
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try {
            runTest { reprocessFromCard() }
        } finally {
            Dispatchers.resetMain()
        }
    }

    // a scheduler that accepts a part of the recording and refuses the whole one
    private suspend fun TestScope.reprocessFromCard() {
        val root = tmp.newFolder("Mnema")
        val dir = File(root, "2026-10-21_14-35").apply { mkdirs() }
        WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(1.0))
        val part = File(root, "2026-10-21_14-35 (0.00.10–0.01.35)")
        val scheduler = object : ProcessingScheduler {
            val asked = mutableListOf<ReprocessOptions>()

            override fun enqueue(dir: File, summaryOnly: Boolean) = true

            override fun reprocess(dir: File, options: ReprocessOptions): File? {
                asked += options
                return if (options.startS == null) null else part
            }

            override fun cancel(dir: File) = Unit
        }
        val store = FolderRecordingStore(root, backgroundScope, { null }, { false }, Dispatchers.Unconfined)
        val model = RecordingViewModel(store, scheduler, root, SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)), io = Dispatchers.Unconfined)
        backgroundScope.launch { model.state.collect {} }
        store.refresh()
        model.state.first { it is DetailState.Shown }

        model.reprocess(ReprocessOptions(lang = Lang.RU, startS = 10.0, endS = 95.0))
        assertEquals(part, model.opened.first { it != null })
        model.openedShown()

        model.reprocess(ReprocessOptions(lang = Lang.RU))
        assertEquals("Не удалось начать обработку", model.failure.first { it != null })
        assertNull(model.opened.value)
        assertEquals(2, scheduler.asked.size)
    }

    @Test
    fun nameSearchAndSectionsNeverSearchTranscriptOrSummary() {
        val work = item(title = "Релиз").copy(section = "Работа", about = "Секретное слово")
        val home = item(title = "Звонок").copy(section = null)
        assertEquals(listOf(work), filterRecordings(listOf(work, home), " РЕЛ ", "Работа"))
        assertTrue(filterRecordings(listOf(work, home), "секретное", null).isEmpty())
        assertEquals(listOf(home), filterRecordings(listOf(work, home), "", ""))
        assertTrue(filterRecordings(listOf(work, home), "", "Пустой").isEmpty())
        assertEquals(listOf(work, home), filterRecordings(listOf(work, home), "", null))
    }

    @Test
    fun followerUsesLibraryIdentityAcrossSectionMoveAndRejectsReplacementAtOldPath() {
        val root = tmp.newFolder("Mnema")
        val start = File(root, "Один").apply { mkdirs() }
        File(start, "transcript.txt").writeText("mic: текст")
        Meta.ensureRecording(start, "library-id") { Meta.isoNow(1000L, java.util.TimeZone.getTimeZone("UTC")) }
        ProcessStateFile.write(start, ProcessState("different-process-id", stage = ProcessStage.DONE))
        val follower = FolderFollower(root, start)
        assertEquals(start, follower.current())
        val moved = File(root, "Работа/Новое имя").apply { parentFile!!.mkdirs() }
        assertTrue(start.renameTo(moved))
        start.mkdir()
        File(start, "transcript.txt").writeText("другая запись")
        Meta.ensureRecording(start, "replacement-id") { Meta.isoNow(1000L, java.util.TimeZone.getTimeZone("UTC")) }
        assertEquals(moved, follower.current())
        assertEquals(moved, FolderFollower(root, start, recording = "library-id").current())
        moved.deleteRecursively()
        assertNull(follower.current())
        assertNull(FolderFollower(root, start, recording = "library-id").current())
    }

    @Test
    fun followerKeepsTheSameFolderWhenFirstPublicationReplacesItsId() {
        val root = tmp.newFolder("Mnema")
        val start = File(root, "Один").apply { mkdirs() }
        File(start, "transcript.txt").writeText("mic: текст")
        Meta.ensureRecording(start, "local-id") { Meta.isoNow(1000L, java.util.TimeZone.getTimeZone("UTC")) }
        val follower = FolderFollower(root, start)
        val meta = File(start, Meta.FILE)
        meta.writeText(meta.readText().replace("local-id", "published-id"))
        assertEquals(start, follower.current())
        val moved = File(root, "Переименовано")
        assertTrue(start.renameTo(moved))
        assertEquals(moved, follower.current())
    }

    @Test
    fun cleanupIsAvailableOnlyForIdleProcessedAudio() {
        val ready = detail(item = item(state = ProcessingState.Done), dialog = listOf(DialogLine("mic", "готово"))).copy(cleanable = true)
        assertTrue(DetailAction.CLEAR_AUDIO in detailActions(ready, true).more)
        assertFalse(DetailAction.CLEAR_AUDIO in detailActions(ready.copy(cleanable = false), true).more)
        assertFalse(DetailAction.CLEAR_AUDIO in detailActions(ready.copy(item = ready.item.copy(recordingNow = true)), true).more)
        assertFalse(DetailAction.CLEAR_AUDIO in detailActions(ready.copy(item = ready.item.copy(state = ProcessingState.Preparing)), true).more)
    }

    @Test
    fun followerAcceptsOnlyRecordingFolders() {
        val root = tmp.newFolder("Mnema")
        val outside = tmp.newFolder("Чужая")
        val nested = File(root, "2026-10-21_14-35/внутри").apply { mkdirs() }

        assertNull(FolderFollower(root, outside).current())
        assertNull(FolderFollower(root, File(root, "../Чужая")).current())
        assertNull(FolderFollower(root, nested).current())
        assertNull(FolderFollower(root, root).current())
        assertNull(FolderFollower(root, File(root, "нет такой")).current())
        assertNull(FolderFollower(root, File(root, ".скрытая").apply { mkdirs() }).current())
    }
}
