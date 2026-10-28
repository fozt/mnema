package dev.mnema.app.recordings

import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.model.DialogLine
import dev.mnema.app.model.Summary
import dev.mnema.app.processing.ProcessStage
import dev.mnema.app.processing.ProcessState
import dev.mnema.app.processing.ProcessStateFile
import dev.mnema.app.processing.TrackState
import dev.mnema.app.sync.NativeTrash
import dev.mnema.app.transcript.Meta
import java.util.TimeZone
import kotlinx.serialization.json.Json
import dev.mnema.app.sync.resource
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import dev.mnema.app.transcript.RecordingFolder
import dev.mnema.app.transcript.WavFixture
import dev.mnema.app.ui.recordings.DetailAction
import dev.mnema.app.ui.recordings.detailActions
import dev.mnema.app.ui.recordings.recordingWarning
import dev.mnema.app.ui.recordings.reportFiles
import java.io.File
import java.time.LocalDateTime
import java.time.ZoneId
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class FolderRecordingStoreTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val root: File get() = File(tmp.root, "Mnema").apply { mkdirs() }
    private var recordingNow: String? = null
    private var working: (String) -> Boolean = { true }

    private fun store() =
        FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), { recordingNow }, { working(it) }, Dispatchers.Unconfined, trash = { assertTrue(it.renameTo(File(tmp.root, "trashed-${it.name}"))) })

    private fun epoch(text: String): Long =
        LocalDateTime.parse(text).atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()

    /** A folder with a [seconds]-second track finished at [endedAt]. */
    private fun folder(name: String, seconds: Double = 2.0, endedAt: String = "2026-10-21T14:40:00", vararg files: Pair<String, String>): File {
        val dir = File(root, name).apply { mkdirs() }
        val wav = WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(seconds))
        wav.setLastModified(epoch(endedAt))
        files.forEach { (file, text) -> File(dir, file).writeText(text) }
        return dir
    }

    // own work of a recording: the id is the recording id in meta, the marker is its current history
    private fun withWork(dir: File, state: ProcessState): ProcessState {
        Meta.ensureRecording(dir, state.id) { null }
        return state.copy(id = recordingId(dir)!!, since = outcomeKey(Meta.read(dir))).also { ProcessStateFile.write(dir, it) }
    }

    private fun RecordingStore.byName(): Map<String, RecordingItem> {
        refresh()
        return items.value.associateBy { it.dir.name }
    }

    @Test
    fun itemsCarryTitleLengthAndStateNewestFirst() {
        folder("2026-10-21_14-35", seconds = 3.0, endedAt = "2026-10-21T14:40:00")
        folder(
            "2026-10-20_Дейли",
            endedAt = "2026-10-20T10:30:00",
            files = arrayOf(
                RecordingFolder.TRANSCRIPT to "mic: привет\n",
                RecordingFolder.SUMMARY to "Обсудили задачи.\n\n- Первый\n- Второй\n",
            ),
        )
        folder(
            "2026-10-19_Звонок",
            endedAt = "2026-10-19T09:00:00",
            files = arrayOf(RecordingFolder.TRANSCRIPT to "mic: да\n"),
        )
        File(root, "не запись").mkdirs()
        File(root, ".скрытая").mkdirs()

        val store = store()
        store.refresh()
        val items = store.items.value

        assertEquals(listOf("2026-10-21_14-35", "2026-10-20_Дейли", "2026-10-19_Звонок"), items.map { it.dir.name })
        val (fresh, daily, call) = items
        assertTrue(fresh.timeTitle)
        assertEquals(3.0, fresh.durationS, 0.001)
        assertEquals(ProcessingState.Unprocessed, fresh.state)
        assertEquals(epoch("2026-10-21T14:35:00"), fresh.startedAt)
        assertEquals("Дейли", daily.title)
        assertEquals("Обсудили задачи.", daily.about)
        assertEquals(ProcessingState.Done, daily.state)
        assertEquals("Звонок", call.title)
        assertTrue(call.state is ProcessingState.NoSummary)
    }

    @Test
    fun folderNamesGiveTheSharedTitlesLikeTheDesktop() {
        val cases = Json.parseToJsonElement(resource("folder-titles-v1.json")).jsonObject.getValue("cases").jsonArray.map { it.jsonObject }
        cases.forEach { folder(it.getValue("folder").jsonPrimitive.content) }
        val items = store().byName()
        for (case in cases) {
            val name = case.getValue("folder").jsonPrimitive.content
            val title = case.getValue("title").jsonPrimitive.contentOrNull
            val item = items.getValue(name)
            assertEquals(name, title == null, item.timeTitle)
            if (title == null) {
                assertEquals(name, case["part"]?.jsonPrimitive?.content, item.partLabel)
            } else {
                assertEquals(name, title, item.title)
                assertEquals(name, title, recordingTitle(item.dir))
            }
        }
    }

    @Test
    fun stateFollowsTheProcessingFile() {
        fun withStage(name: String, stage: String, summaryOnly: Boolean = false, job: String? = null, problem: String? = null) =
            withWork(folder(name), ProcessState(id = name, stage = stage, summaryOnly = summaryOnly, job = job, problem = problem))
        withStage("2026-10-21_10-00", ProcessStage.ASR, job = "job")
        withStage("2026-10-21_10-01", ProcessStage.ASR)
        withStage("2026-10-21_10-02", ProcessStage.SUMMARY)
        withStage("2026-10-21_10-03", ProcessStage.NEW, summaryOnly = true)
        withStage("2026-10-21_10-04", ProcessStage.FAILED, problem = "Сервер не принял токен.")

        val items = store().byName()

        assertTrue(items.getValue("2026-10-21_10-00").state is ProcessingState.Recognizing)
        assertEquals("no job yet, the audio is being prepared", ProcessingState.Preparing, items.getValue("2026-10-21_10-01").state)
        assertEquals(ProcessingState.Summarizing, items.getValue("2026-10-21_10-02").state)
        assertEquals(ProcessingState.Summarizing, items.getValue("2026-10-21_10-03").state)
        assertEquals(ProcessingState.Failed("Сервер не принял токен."), items.getValue("2026-10-21_10-04").state)
    }

    @Test
    fun recordingKeptWithoutAServerIsAudioOnlyNotAFailure() {
        val dir = folder("2026-10-21_10-00")
        withWork(dir, ProcessState(id = "a", stage = ProcessStage.AUDIO_ONLY, problem = "Сервер обработки не настроен. Сохранён только звук."))
        working = { false }

        val store = store()
        val item = store.byName().getValue("2026-10-21_10-00")
        val detail = runBlocking { store.detail(dir) }!!

        assertEquals(ProcessingState.AudioOnly("Сервер обработки не настроен. Сохранён только звук."), item.state)
        assertEquals(listOf(File(dir, "mic.wav")), detail.tracks)
        assertTrue(detail.processable)
    }

    @Test
    fun unfinishedStageWithoutWorkIsAnInterruptedProcessing() {
        fun orphan(name: String, stage: String, vararg files: Pair<String, String>) =
            withWork(folder(name, files = files), ProcessState(id = name, stage = stage))
        orphan("2026-10-21_10-00", ProcessStage.ASR)
        orphan("2026-10-21_10-01", ProcessStage.SUMMARY, RecordingFolder.TRANSCRIPT to "mic: a\n")
        orphan("2026-10-21_10-02", ProcessStage.ASR, RecordingFolder.SUMMARY to "Суть.\n\n- один\n")
        orphan("2026-10-21_10-03", ProcessStage.NEW)
        // an outcome arrived without the interrupted work (synced from the desktop): the state follows history
        orphan("2026-10-21_10-04", ProcessStage.ASR, RecordingFolder.TRANSCRIPT to "mic: a\n")
            .also { Meta.appendRun(File(root, "2026-10-21_10-04"), Json.parseToJsonElement("""{"status":"ok"}""").jsonObject) }
        working = { it == "2026-10-21_10-03" }

        val items = store().byName()

        // like the desktop: interrupted means unprocessed, and processing continues it
        assertEquals(ProcessingState.Unprocessed, items.getValue("2026-10-21_10-00").state)
        assertEquals(ProcessingState.Unprocessed, items.getValue("2026-10-21_10-01").state)
        assertEquals(ProcessingState.Unprocessed, items.getValue("2026-10-21_10-02").state)
        assertEquals(ProcessingState.Preparing, items.getValue("2026-10-21_10-03").state)
        assertTrue(items.getValue("2026-10-21_10-04").state is ProcessingState.NoSummary)
    }

    @Test
    fun detailTellsWhatTheSchedulerAccepts() = runBlocking {
        val full = folder("2026-10-21_14-35", files = arrayOf(RecordingFolder.TRANSCRIPT to "mic: a\n"))
        val sound = folder("2026-10-21_15-00")
        val text = File(root, "2026-10-21_Дейли").apply { mkdirs() }
        File(text, RecordingFolder.TRANSCRIPT).writeText("mic: a\n")
        val blank = folder("2026-10-21_16-00", files = arrayOf(RecordingFolder.TRANSCRIPT to " \n"))
        val store = store()

        val (fullDetail, soundDetail, textDetail, blankDetail) = listOf(full, sound, text, blank).map { store.detail(it)!! }

        assertTrue(fullDetail.processable)
        assertTrue(fullDetail.summarizable)
        assertTrue(soundDetail.processable)
        assertFalse(soundDetail.summarizable)
        assertFalse(textDetail.processable)
        assertTrue(textDetail.summarizable)
        assertFalse("a transcript of only spaces", blankDetail.summarizable)
    }

    @Test
    fun liveProgressOverridesTheFileUntilCleared() {
        val dir = folder("2026-10-21_14-35")
        val store = store()
        store.refresh()

        store.report(dir, ProcessingState.Recognizing(0.4, "распознаю"))
        assertEquals(ProcessingState.Recognizing(0.4, "распознаю"), store.items.value.single().state)

        store.report(dir, null)
        assertEquals(ProcessingState.Unprocessed, store.items.value.single().state)
    }

    @Test
    fun betweenStepsTheRecordingKeepsUploadAndServerProgress() {
        val dir = folder("2026-10-21_14-35")
        val sending = withWork(dir, ProcessState("id", stage = ProcessStage.ASR, tracks = mapOf("mic" to TrackState(key = "k", uploadId = "u1", uploaded = 10))))
        val store = store()
        assertEquals("after a restart there is no share, but it is still uploading", ProcessingState.Uploading(null), store.byName().values.single().state)

        store.report(dir, ProcessingState.Uploading(0.4))
        store.report(dir, null)
        assertEquals(ProcessingState.Uploading(0.4), store.byName().values.single().state)

        ProcessStateFile.write(dir, sending.copy(job = "job"))
        store.report(dir, ProcessingState.Recognizing(0.7, "Распознаю"))
        store.report(dir, null)
        assertEquals(ProcessingState.Recognizing(0.7, "Распознаю"), store.byName().values.single().state)

        ProcessStateFile.write(dir, sending.copy(job = "job", stage = ProcessStage.NEW))
        assertEquals("a retry continues the server job", ProcessingState.Recognizing(0.7, "Распознаю"), store.byName().values.single().state)
    }

    @Test
    fun folderBeingRecordedIsMarked() {
        folder("2026-10-21_14-35")
        recordingNow = "2026-10-21_14-35"

        assertTrue(store().byName().getValue("2026-10-21_14-35").recordingNow)
    }

    @Test
    fun emptyTrackOfAFailedStartIsListedWithZeroLength() {
        val dir = File(root, "2026-10-21_14-35").apply { mkdirs() }
        File(dir, "mic.wav").writeBytes(WavFixture.header(0))

        val item = store().byName().getValue("2026-10-21_14-35")

        assertEquals(0.0, item.durationS, 0.0)
    }

    @Test
    fun detailHasSummaryDialogTracksAndRemarks() = runBlocking {
        val dir = folder(
            "2026-10-21_Дейли",
            files = arrayOf(
                RecordingFolder.TRANSCRIPT to "mic: Привет.\nsystem: Здравствуйте.\n",
                RecordingFolder.SUMMARY to "Суть.\n\n- один\n- два\n",
            ),
        )
        WavFixture.write(File(dir, "system.wav"), WavFixture.samples(1.0))
        Meta.appendRun(dir, Json.parseToJsonElement("""{"remarks":["Дорожка не распознана."]}""").jsonObject)

        val detail = store().detail(dir)!!

        assertEquals(Summary("Суть.", listOf("один", "два")), detail.summary)
        assertEquals(listOf(DialogLine("mic", "Привет."), DialogLine("system", "Здравствуйте.")), detail.dialog)
        assertEquals(listOf(File(dir, "mic.wav"), File(dir, "system.wav")), detail.tracks)
        assertEquals(listOf("Дорожка не распознана."), detail.remarks)
        assertEquals("Дейли", detail.item.title)
    }

    @Test
    fun detailOfAFolderWithoutFilesIsBare() = runBlocking {
        val dir = folder("2026-10-21_14-35")
        File(dir, "system.wav").writeBytes(WavFixture.header(0))

        val detail = store().detail(dir)!!

        assertNull(detail.summary)
        assertEquals(emptyList<DialogLine>(), detail.dialog)
        assertEquals(listOf(File(dir, "mic.wav")), detail.tracks)
        assertNull(store().detail(File(root, "нет такой")))
    }

    @Test
    fun deleteRemovesTheFolderAndRefusesOthers() = runBlocking {
        val dir = folder("2026-10-21_14-35", files = arrayOf(RecordingFolder.TRANSCRIPT to "mic: a\n"))
        val recorded = folder("2026-10-21_15-00")
        val outside = File(tmp.root, "чужая").apply { mkdirs() }
        File(outside, "mic.wav").writeText("x")
        recordingNow = recorded.name
        val store = store()
        store.refresh()

        assertTrue(store.delete(dir))
        assertFalse(dir.exists())
        assertEquals(listOf(recorded.name), store.items.value.map { it.dir.name })
        assertFalse(store.delete(recorded))
        assertTrue(recorded.exists())
        assertFalse(store.delete(outside))
        assertTrue(outside.exists())
    }

    @Test
    fun withoutAccessToAllFilesARecordingIsNotPartlyDeleted() = runBlocking {
        val dir = folder("2026-10-21_14-35", files = arrayOf(RecordingFolder.TRANSCRIPT to "mic: a\n"))
        val system = NativeTrash(37, { false }, { File(tmp.root, "probe") }) { error("the system is not asked without access") }
        val store = FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), { null }, { false }, Dispatchers.Unconfined,
            trash = system::move, hasTrash = system::available)

        store.detail(dir)
        assertTrue("the dialogs do not offer deleting forever", store.trashAvailable.value)
        assertFalse(store.delete(dir, forever = true))

        assertEquals("Разрешите доступ ко всем файлам для перемещения в корзину", store.failure.value)
        assertEquals(setOf("mic.wav", RecordingFolder.TRANSCRIPT, ".mnema-meta.json"), dir.list()!!.toSet())
    }

    @Test fun onlyAnUnavailableNodeFailureOffersChoosingANode() = runBlocking {
        val down = folder("2026-10-22_16-01")
        withWork(down, ProcessState("a", stage = ProcessStage.FAILED, problem = "Mac mini недоступен", unreachable = true))
        val token = folder("2026-10-22_16-02")
        withWork(token, ProcessState("b", stage = ProcessStage.FAILED, problem = "Сервер не принял токен"))
        val retried = folder("2026-10-22_16-03")
        withWork(retried, ProcessState("c", stage = ProcessStage.NEW, unreachable = true))
        working = { false }
        assertTrue(store().detail(down)!!.nodeDown)
        assertFalse(store().detail(token)!!.nodeDown)
        assertFalse(store().detail(retried)!!.nodeDown)
    }

    @Test fun dateAndTimeComeFromTheFolderNameBeforeMetadataAndTracks() = runBlocking {
        val dir = folder("2026-10-22_16-01")
        Meta.ensureRecording(dir, "id") { Meta.isoNow(epoch("2026-10-22T16:01:07"), TimeZone.getDefault()) }
        ProcessStateFile.write(dir, ProcessState("id", stage = ProcessStage.DONE))
        val item = store().detail(dir)!!.item
        assertEquals("16:01", item.title)
        assertTrue(item.timeTitle)
        assertEquals(epoch("2026-10-22T16:01:00"), item.startedAt)
        val imported = folder("2020-01-01_Имя с ПК")
        assertEquals("Имя с ПК", store().detail(imported)!!.item.title)
        assertEquals(epoch("2020-01-01T14:39:58"), store().detail(imported)!!.item.startedAt)
    }

    @Test fun newProcessingHidesRemarksOfThePreviousOne() = runBlocking {
        val dir = folder("Запись")
        val stale = "Сервер обработки не настроен. Сохранён только звук."
        Meta.appendRun(dir, Json.parseToJsonElement("""{"status":"ok","remarks":["$stale"]}""").jsonObject)
        val store = store()
        assertEquals(listOf(stale), store.detail(dir)!!.remarks)

        withWork(dir, ProcessState("id", stage = ProcessStage.NEW))
        val preparing = store.detail(dir)!!
        assertEquals(ProcessingState.Preparing, preparing.item.state)
        assertEquals(emptyList<String>(), preparing.remarks)
        store.report(dir, ProcessingState.Uploading(0.3))
        assertEquals("the audio is still uploading", emptyList<String>(), store.detail(dir)!!.remarks)
        store.report(dir, ProcessingState.Recognizing(0.4, "Распознаю"))
        assertEquals(emptyList<String>(), store.detail(dir)!!.remarks)

        store.report(dir, null)
        Meta.appendRun(dir, Json.parseToJsonElement("""{"status":"ok","remarks":["Дорожка тихая"]}""").jsonObject)
        withWork(dir, ProcessState("id", stage = ProcessStage.DONE))
        assertEquals(listOf("Дорожка тихая"), store.detail(dir)!!.remarks)
    }

    @Test fun nodeRemarksFollowCurrentProblem() = runBlocking {
        val dir = folder("Запись")
        Meta.appendRun(dir, Json.parseToJsonElement("""{"settings":{"source":"node"},"remarks":["Модель не ответила","Дорожка тихая"]}""").jsonObject)
        withWork(dir, ProcessState("id", stage = ProcessStage.FAILED, problem = "Узел недоступен"))
        val detail = store().detail(dir)!!
        assertEquals(listOf("Узел недоступен", "Модель не ответила", "Дорожка тихая"), detail.remarks)
        assertEquals(ProcessingState.Failed("Узел недоступен"), detail.item.state)
    }


    @Test fun processedRecordingWithoutSpeechIsNoSpeechAndCanBeProcessedAgain() = runBlocking {
        val dir = folder("2026-10-21_10-00")
        WavFixture.write(File(dir, "system.wav"), WavFixture.samples(2.0))
        // the server decided both tracks are silent: processing finished without text
        Meta.appendRun(dir, Json.parseToJsonElement("""{"status":"ok","job":"j1","remarks":["На дорожке «Микрофон» нет звука."]}""").jsonObject)
        withWork(dir, ProcessState("id", stage = ProcessStage.DONE))
        working = { false }
        val detail = store().detail(dir)!!
        assertEquals(ProcessingState.NoSpeech, detail.item.state)
        assertEquals(DetailAction.REPROCESS, detailActions(detail, serverSet = true).lead)
    }

    @Test fun successFromTheDesktopAfterAFailureOnThePhoneIsProcessedHere() = runBlocking {
        val dir = folder("2026-10-21_10-00")
        withWork(dir, ProcessState("id", stage = ProcessStage.FAILED, problem = "Mac mini недоступен", unreachable = true,
            job = "phone-job", key = "phone-key"))
        working = { false }
        val store = store()
        assertEquals(ProcessingState.Failed("Mac mini недоступен"), store.detail(dir)!!.item.state)

        // sync brought a successful run from the desktop
        File(dir, RecordingFolder.TRANSCRIPT).writeText("mic: Привет\n")
        File(dir, RecordingFolder.SUMMARY).writeText("Суть.\n\n- пункт\n")
        Meta.appendRun(dir, Json.parseToJsonElement("""{"status":"ok","job":"pc-job","finished":"2026-10-21T11:00:00+03:00"}""").jsonObject)

        val detail = store.detail(dir)!!
        assertEquals(ProcessingState.Done, detail.item.state)
        assertFalse(detail.nodeDown || detail.retryContinues)
        assertTrue(DetailAction.RETRY !in detailActions(detail, serverSet = true).let { listOfNotNull(it.lead) + it.more })
    }

    @Test fun stoppedProcessingLeavesOnlyTheSoundOrTheEarlierResult() = runBlocking {
        val fresh = folder("2026-10-21_10-00")
        val done = folder("2026-10-21_11-00", files = arrayOf(RecordingFolder.TRANSCRIPT to "mic: Привет\n"))
        Meta.appendRun(done, Json.parseToJsonElement("""{"status":"ok","job":"j1"}""").jsonObject)
        for (dir in listOf(fresh, done)) {
            withWork(dir, ProcessState("id-${dir.name}", stage = ProcessStage.ASR, job = "job"))
            assertTrue(dev.mnema.app.processing.stopProcessing(root, dir, cancel = {}))
        }
        working = { false }
        val items = store().byName()
        assertTrue(items.getValue(fresh.name).state is ProcessingState.AudioOnly)
        assertTrue(items.getValue(done.name).state is ProcessingState.NoSummary)
    }

    @Test fun aCopyMadeMidProcessingIsACopyAndTheOriginalGoesOn() = runBlocking {
        val original = folder("2026-10-21_10-00")
        val state = withWork(original, ProcessState("id", stage = ProcessStage.ASR))
        Thread.sleep(10)
        val copy = File(root, "2026-10-21_10-00 (2)").also { original.copyRecursively(it) }
        working = { it == state.id }

        // a processing step finds the folder by recording id, so the copy gets its own
        assertEquals(original, dev.mnema.app.processing.WorkScheduler.findFolder(root, state.id))
        val items = store().byName()
        assertEquals(ProcessingState.Preparing, items.getValue(original.name).state)
        assertEquals(ProcessingState.Unprocessed, items.getValue(copy.name).state)
        assertTrue(items.getValue(original.name).id != items.getValue(copy.name).id)
    }

    @Test fun transcriptOnlyImportKeepsTextAndAssignsIdentityOnlyOnce() = runBlocking {
        val dir = File(root, "2020-01-01_Импорт").apply { mkdirs() }
        val transcript = File(dir, RecordingFolder.TRANSCRIPT).apply { writeText("mic: Привет") }
        assertTrue(transcript.setLastModified(epoch("2026-10-22T15:45:00")))
        val original = transcript.readBytes()
        val store = store()
        val first = store.detail(dir)!!.item
        assertEquals("the date comes from the name, the time from the file", epoch("2020-01-01T15:45:00"), first.startedAt)
        assertTrue(first.id != null)
        val metadata = File(dir, Meta.FILE).readBytes()
        assertTrue(transcript.setLastModified(epoch("2026-10-23T20:00:00")))
        assertEquals(first, store.detail(dir)!!.item)
        assertTrue(metadata.contentEquals(File(dir, Meta.FILE).readBytes()))
        assertTrue(original.contentEquals(transcript.readBytes()))
        val known = File(root, "Известное начало").apply { mkdirs() }
        File(known, RecordingFolder.TRANSCRIPT).writeText("mic: текст")
        Meta.ensureRecording(known, "id") { Meta.isoNow(epoch("2026-10-21T16:01:00"), TimeZone.getDefault()) }
        assertEquals(epoch("2026-10-21T16:01:00"), store.detail(known)!!.item.startedAt)
    }

    @Test fun partLabelComesFromTheFolderNameNotProcessingOptions() = runBlocking {
        val part = folder("2026-10-22_16-01 (0.00.10–0.01.35)")
        ProcessStateFile.write(part, ProcessState("id", stage = ProcessStage.FAILED,
            options = dev.mnema.app.processing.ReprocessOptions(startS = 600.0, endS = 700.0)))
        val item = store().detail(part)!!.item
        assertEquals("16:01 (0.00.10–0.01.35)", item.title)
        assertEquals("0.00.10–0.01.35", item.partLabel)
        val whole = folder("2026-10-22_16-02")
        ProcessStateFile.write(whole, ProcessState("id2", stage = ProcessStage.FAILED,
            options = dev.mnema.app.processing.ReprocessOptions(startS = 10.0, endS = 95.0)))
        assertNull(store().detail(whole)!!.item.partLabel)
    }

    @Test fun summaryFailureCanBeRetriedWithAnOldSummaryAndNoAudio() = runBlocking {
        val dir = File(root, "Моё название").apply { mkdirs() }
        File(dir, RecordingFolder.TRANSCRIPT).writeText("mic: Привет")
        File(dir, RecordingFolder.SUMMARY).writeText("Старая суть.\n\n- Старый пункт")
        Meta.appendRun(dir, Json.parseToJsonElement("""{"kind":"summary","status":"failed","error":"Лимит модели",
            "remarks":["Лимит модели"],"notes":[{"step":"summary","cause":"Лимит модели"}]}""").jsonObject)
        val detail = store().detail(dir)!!
        // a summary run does not change the outcome: the recording is processed, the summary can be retried
        assertEquals(ProcessingState.Done, detail.item.state)
        assertTrue(detail.retrySummaryOnly)
        assertTrue(detail.summarizable)
        assertFalse(detail.processable)
        assertEquals(listOf("Старый пункт"), detail.summary!!.points)
    }

    @Test fun summaryMdOfAnEarlierVersionIsRenamedWhenTheListReadsIt() {
        val dir = folder("2026-10-20_Звонок", files = arrayOf(RecordingFolder.TRANSCRIPT to "mic: Привет", "summary.md" to "Суть звонка.\n\n- пункт\n"))
        val both = folder("2026-10-19_Обе", files = arrayOf("summary.md" to "старая", "summary.txt" to "новая"))
        val indexed = mutableListOf<String>()
        MediaIndex.scanner = { files, _ -> files.mapTo(indexed) { it.name } }
        try {
            assertEquals("Суть звонка.", store().byName().getValue(dir.name).about)
        } finally { MediaIndex.scanner = null }
        assertEquals("Суть звонка.\n\n- пункт\n", File(dir, "summary.txt").readText())
        assertFalse(File(dir, "summary.md").exists())
        assertEquals(listOf("summary.md", "summary.txt"), indexed.filter { it.startsWith("summary") }) // the old path leaves Android's index, the new one enters it
        assertEquals("старая", File(both, "summary.md").readText()) // summary.txt wins, nothing is lost
        assertEquals("новая", File(both, "summary.txt").readText())
    }

    @Test fun titleAndroidDidNotApplyIsShownButDoesNotOfferAnotherSummary() = runBlocking {
        val dir = folder("Запись", files = arrayOf(RecordingFolder.TRANSCRIPT to "mic: Привет", "summary.txt" to "Итог."))
        val remark = "Название «Дейли» не применено: Android не дал переименовать папку. Нажмите «Переименовать» в меню записи."
        Meta.appendRun(dir, Json.parseToJsonElement(
            """{"status":"ok","notes":[{"step":"title","cause":"x","code":"title_not_applied"}],"remarks":["$remark"]}""",
        ).jsonObject)
        val detail = store().detail(dir)!!
        assertEquals(listOf(remark), detail.remarks)
        assertFalse(detail.retrySummaryOnly)
    }

    @Test fun pendingFullRetryDoesNotUseOldSummaryFailureAsItsOperation() = runBlocking {
        val dir = folder("Запись", files = arrayOf(RecordingFolder.TRANSCRIPT to "mic: Привет"))
        Meta.appendRun(dir, Json.parseToJsonElement("""{"status":"ok","notes":[{"step":"title"}]}""").jsonObject)
        assertTrue(store().detail(dir)!!.retrySummaryOnly)
        withWork(dir, ProcessState("id", stage = ProcessStage.FAILED, problem = "Узел недоступен", key = "lost-request"))
        assertFalse(store().detail(dir)!!.retrySummaryOnly)
    }


    @Test fun summaryOnlyFolderHasItsNameTextDateAndNoSummarizeAction() = runBlocking {
        val dir = File(root, "2020-01-01_Импорт").apply { mkdirs() }
        val summary = File(dir, RecordingFolder.SUMMARY).apply { writeText("Суть.\n\n- План") }
        assertTrue(summary.setLastModified(epoch("2026-10-22T15:45:00")))
        val detail = store().detail(dir)!!
        assertEquals(epoch("2020-01-01T15:45:00"), detail.item.startedAt)
        assertEquals("Импорт", detail.item.title)
        assertFalse(detail.summarizable)
        assertFalse(detail.processable)
    }

    @Test
    fun otherSideWarningLastsWhileTheLastRunHasItsRemark() {
        val case = resource("silent-reason-v1.json").let(Json::parseToJsonElement).jsonObject.getValue("cases").jsonArray
            .map { it.jsonObject }.first { it["reason"]?.jsonPrimitive?.contentOrNull == "blocked" }
        val note = """{"step":"asr","cause":${case["cause"]},"short":${case["short"]},"code":${case["code"]}}"""
        val dir = folder("2026-10-25_10-00", files = arrayOf(
            RecordingFolder.TRANSCRIPT to "mic: алло\n",
            Meta.FILE to """{"version":1,"id":"r1","runs":[{"status":"ok","notes":[$note],"remarks":[${case["cause"]}]}]}""",
        ))

        val warned = store().byName().getValue(dir.name)
        assertEquals(case["short"]!!.jsonPrimitive.content, warned.warning)
        assertEquals(warned.warning, recordingWarning(warned))
        assertNull("the recording is being processed again", recordingWarning(warned.copy(state = ProcessingState.Preparing)))
        Meta.appendRun(dir, Json.parseToJsonElement("""{"kind":"process","status":"stopped"}""").jsonObject)
        assertEquals("nothing to say about a stopped run: the remark of the run before it", warned.warning,
            store().byName().getValue(dir.name).warning)

        File(dir, Meta.FILE).writeText(
            """{"version":1,"id":"r1","runs":[{"status":"ok","notes":[$note]},{"status":"ok","notes":[{"step":"asr","short":"Нет речи"}]}]}""",
        )
        assertNull("the new run has no remark", store().byName().getValue(dir.name).warning)
    }

    @Test
    fun cardOffersTheHelperReportOnlyWhenTheRecordingHasIt() = runBlocking {
        val plain = folder("2026-10-25_11-00", files = arrayOf(RecordingFolder.TRANSCRIPT to "mic: алло\n"))
        val reported = folder("2026-10-25_12-00", files = arrayOf(
            RecordingFolder.TRANSCRIPT to "mic: алло\n",
            HelperProtocol.REPORT to """{"status":"stopped"}""",
            HelperProtocol.LOG_TAIL to "2026-10-25 12:00:00.000 Команда stop\n",
        ))
        val store = store()

        val without = store.detail(plain)!!
        val with = store.detail(reported)!!

        assertFalse(DetailAction.SHARE_REPORT in detailActions(without, serverSet = true).more)
        assertTrue(DetailAction.SHARE_REPORT in detailActions(with, serverSet = true).more)
        assertTrue(reportFiles(plain).isEmpty())
        assertEquals(listOf(HelperProtocol.REPORT, HelperProtocol.LOG_TAIL), reportFiles(reported).map { it.source.name })
        File(reported, HelperProtocol.LOG_TAIL).delete()
        assertEquals("no log, only the report", listOf(HelperProtocol.REPORT), reportFiles(reported).map { it.source.name })
    }
}

