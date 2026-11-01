package dev.mnema.app.ui

import androidx.lifecycle.SavedStateHandle
import dev.mnema.app.model.Lang
import dev.mnema.app.processing.ProcessingScheduler
import dev.mnema.app.processing.ProcessStage
import dev.mnema.app.processing.ProcessState
import dev.mnema.app.processing.ProcessStateFile
import dev.mnema.app.processing.stopped
import dev.mnema.app.processing.ReprocessOptions
import dev.mnema.app.recordings.FolderRecordingStore
import dev.mnema.app.recordings.outcomeKey
import dev.mnema.app.transcript.Meta
import dev.mnema.app.transcript.RecordingFolder
import dev.mnema.app.ui.recordings.RecordingsViewModel
import java.time.ZoneId
import kotlin.time.Duration.Companion.seconds
import dev.mnema.app.transcript.WavFixture
import java.util.TimeZone
import dev.mnema.app.ui.recordings.DetailState
import dev.mnema.app.ui.recordings.DetailAction
import dev.mnema.app.ui.recordings.isProcessing
import dev.mnema.app.ui.recordings.RecordingViewModel
import java.io.File
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.assertFalse
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

@OptIn(ExperimentalCoroutinesApi::class)
class RecordingViewModelTest {
    @get:Rule
    val tmp = TemporaryFolder()

    @Before
    fun setUp() = Dispatchers.setMain(UnconfinedTestDispatcher())

    @After
    fun tearDown() = Dispatchers.resetMain()

    // a WorkManager scheduler that did not accept the recording
    private class Refusing : ProcessingScheduler {
        val asked = mutableListOf<Boolean>()

        override fun enqueue(dir: File, summaryOnly: Boolean): Boolean {
            asked += summaryOnly
            return false
        }

        override fun reprocess(dir: File, options: ReprocessOptions): File? = null

        override fun cancel(dir: File) = Unit
    }

    @Test
    fun refusedProcessingIsExplained() = runTest {
        val root = tmp.newFolder("Mnema")
        val dir = File(root, "2026-10-21_14-35").apply { mkdirs() }
        WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(1.0))
        val store = FolderRecordingStore(root, backgroundScope, { null }, { false }, Dispatchers.Unconfined)
        val scheduler = Refusing()
        val model = RecordingViewModel(store, scheduler, root, SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)), io = Dispatchers.Unconfined)
        backgroundScope.launch { model.state.collect {} }
        store.refresh()
        model.state.first { it is DetailState.Shown }

        model.retry()

        assertEquals("Запись нельзя обработать: нет звука", model.failure.first { it != null })
        assertEquals(listOf(false), scheduler.asked)
    }

    private fun recording(root: File, name: String): File = File(root, name).apply {
        mkdirs()
        WavFixture.write(File(this, "mic.wav"), WavFixture.samples(1.0))
    }

    @Test
    fun theRecordingsScreenRereadsItsRecordingNotTheLibrary() = runTest {
        val root = tmp.newFolder("Mnema")
        val dir = recording(root, "2026-10-21_14-35")
        val store = FolderRecordingStore(root, backgroundScope, { null }, { false }, Dispatchers.Unconfined)
        store.refresh()
        store.loaded.first { it }
        val model = RecordingViewModel(store, Refusing(), root, SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)),
            io = Dispatchers.Unconfined, clock = testScheduler.timeSource)
        backgroundScope.launch { model.state.collect {} }
        backgroundScope.launch { model.rereading.whileShown() }
        assertEquals(null, (model.state.first { it is DetailState.Shown } as DetailState.Shown).detail.summary)

        File(dir, RecordingFolder.SUMMARY).writeText("Суть.")
        val another = recording(root, "2026-10-22_09-00")
        // it was just read: the first reread waits a period
        runCurrent()
        assertEquals(null, (model.state.value as DetailState.Shown).detail.summary)
        advanceTimeBy(5.seconds)
        runCurrent()
        assertEquals("Суть.", (model.state.value as DetailState.Shown).detail.summary?.about)
        // the library is not rescanned for it
        assertFalse(store.items.value.any { it.dir.name == another.name })
    }

    @Test
    fun comingBackFromARecordingDoesNotRescanTheListBeforeItIsDue() = runTest {
        val root = tmp.newFolder("Mnema")
        recording(root, "2026-10-21_14-35")
        val store = FolderRecordingStore(root, backgroundScope, { null }, { false }, Dispatchers.Unconfined)
        val list = RecordingsViewModel(store, ZoneId.of("UTC"), compute = Dispatchers.Unconfined, clock = testScheduler.timeSource)
        fun shown() = store.items.value.map { it.dir.name }

        val first = backgroundScope.launch { list.rereading.whileShown() }
        runCurrent()
        assertEquals(listOf("2026-10-21_14-35"), shown()) // the first showing reads at once
        advanceTimeBy(2.seconds)
        first.cancel() // a recording is opened over the list
        recording(root, "2026-10-22_09-00")
        advanceTimeBy(1.seconds)

        val back = backgroundScope.launch { list.rereading.whileShown() }
        runCurrent()
        assertEquals(listOf("2026-10-21_14-35"), shown())
        advanceTimeBy(2.seconds)
        runCurrent()
        assertEquals(listOf("2026-10-22_09-00", "2026-10-21_14-35"), shown()) // five seconds after the last reading
        back.cancel()
    }

    // remembers how processing was queued: as a continuation or with the dialog's choice
    private class Recorded : ProcessingScheduler {
        val enqueued = mutableListOf<Boolean>()
        val chosen = mutableListOf<ReprocessOptions>()
        val asked = CompletableDeferred<Unit>()

        override fun enqueue(dir: File, summaryOnly: Boolean): Boolean {
            enqueued += summaryOnly
            asked.complete(Unit)
            return true
        }

        override fun reprocess(dir: File, options: ReprocessOptions): File {
            chosen += options
            asked.complete(Unit)
            return dir
        }

        override fun cancel(dir: File) = Unit
    }

    private suspend fun kotlinx.coroutines.test.TestScope.failedCard(options: ReprocessOptions?): Pair<RecordingViewModel, Recorded> {
        val root = tmp.newFolder()
        val dir = File(root, "2026-10-21_14-35").apply { mkdirs() }
        WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(1.0, listOf(0.1 to 0.9)))
        Meta.ensureRecording(dir, "run") { null }
        ProcessStateFile.write(dir, ProcessState(id = "run", stage = ProcessStage.FAILED, problem = "Mac mini недоступен",
            job = "job", key = "key", options = options, since = outcomeKey(Meta.read(dir))))
        val store = FolderRecordingStore(root, backgroundScope, { null }, { false }, Dispatchers.Unconfined)
        val scheduler = Recorded()
        val model = RecordingViewModel(store, scheduler, root, SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)), io = Dispatchers.Unconfined)
        backgroundScope.launch { model.state.collect {} }
        store.refresh()
        model.state.first { it is DetailState.Shown }
        return model to scheduler
    }

    @Test
    fun unchangedRetryOfAChosenRunContinuesItsJobAndAChangedRetryStartsWithTheChoice() = runTest {
        val run = ReprocessOptions(lang = Lang.EN, asr = "groq/whisper-large-v3", tracks = setOf("mic"))
        // automatic processing without a choice and processing with the dialog's choice
        for (options in listOf(null, run)) {
            val (card, scheduler) = failedCard(options = options)
            val detail = (card.state.value as DetailState.Shown).detail
            assertTrue("$options", detail.retryContinues)
            assertEquals(options, detail.failedRun)
            card.process(options ?: ReprocessOptions(tracks = setOf("mic")), unchanged = true, retrying = true)
            scheduler.asked.await()
            assertEquals("$options", listOf(false), scheduler.enqueued)
            assertTrue("$options", scheduler.chosen.isEmpty())
        }

        val changed = run.copy(lang = Lang.RU)
        val (again, againScheduler) = failedCard(options = run)
        again.process(changed, unchanged = false, retrying = true)
        againScheduler.asked.await()
        assertEquals(listOf(changed), againScheduler.chosen)
        assertTrue(againScheduler.enqueued.isEmpty())
    }

    @Test
    fun renamedCardFollowsItsFolderAndAProcessedOneIsNamedAtTheEnd() = runTest {
        val root = tmp.newFolder("Mnema")
        val dir = File(root, "2026-10-21_14-35").apply { mkdirs() }
        WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(1.0, listOf(0.1 to 0.9)))
        Meta.ensureRecording(dir, "record") { Meta.isoNow(1792582500000, TimeZone.getTimeZone("GMT+03:00")) }
        var working = false
        val store = FolderRecordingStore(root, backgroundScope, { null }, { working }, Dispatchers.Unconfined)
        val model = RecordingViewModel(store, Recorded(), root, SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)), io = Dispatchers.Unconfined)
        backgroundScope.launch { model.state.collect {} }
        store.refresh()
        model.state.first { it is DetailState.Shown }

        model.rename("Дейли: с командой")

        val renamed = model.state.first { (it as? DetailState.Shown)?.detail?.item?.title == "Дейли с командой" } as DetailState.Shown
        assertEquals("2026-10-21_Дейли с командой", renamed.detail.item.dir.name)
        assertEquals("record", renamed.detail.item.id)
        assertFalse(dir.exists())

        working = true
        ProcessStateFile.write(renamed.detail.item.dir, ProcessState(id = "record", stage = ProcessStage.ASR))
        store.refresh()
        val busy = model.state.first { (it as? DetailState.Shown)?.detail?.item?.state?.let(::isProcessing) == true } as DetailState.Shown
        assertEquals(null, busy.actions.renameBlocked)
        assertEquals(DetailAction.TITLE, busy.actions.naming)
        model.rename("Другое")
        val named = model.state.first { (it as? DetailState.Shown)?.detail?.item?.pendingTitle == "Другое" } as DetailState.Shown
        assertEquals(renamed.detail.item.dir, named.detail.item.dir)
        assertTrue(renamed.detail.item.dir.isDirectory)
        assertEquals("Другое", ProcessStateFile.read(renamed.detail.item.dir)?.userTitle)
    }

    @Test
    fun renameFromTheListRenamesByTheRecordingScreenRule() = runTest {
        val root = tmp.newFolder("Mnema")
        val dir = File(root, "2026-10-21_14-35").apply { mkdirs() }
        WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(1.0, listOf(0.1 to 0.9)))
        Meta.ensureRecording(dir, "record") { Meta.isoNow(1792582500000, TimeZone.getTimeZone("GMT+03:00")) }
        var writing: String? = null
        var working = false
        val store = FolderRecordingStore(root, backgroundScope, { writing }, { working }, Dispatchers.Unconfined)
        val list = RecordingsViewModel(store, ZoneId.of("UTC"), compute = Dispatchers.Unconfined)
        fun card() = store.items.value.single()
        store.refresh()
        store.loaded.first { it }

        list.rename(card(), "Дейли: с командой")
        val renamed = store.items.first { it.singleOrNull()?.title == "Дейли с командой" }.single()
        assertEquals("2026-10-21_Дейли с командой", renamed.dir.name)
        assertEquals("record", renamed.id)
        assertFalse(dir.exists())
        assertEquals(null, list.notice.value)

        // the same title changes nothing; the folder being recorded keeps its name
        list.rename(renamed, " Дейли с командой ")
        writing = renamed.dir.name
        store.refresh()
        val recording = store.items.first { it.single().recordingNow }.single()
        list.rename(recording, "Другое")
        store.refresh()
        runCurrent()
        assertEquals(renamed.dir, card().dir)
        assertTrue(renamed.dir.isDirectory)
        assertEquals(null, list.notice.value)

        // a processing recording gets a title, processing names the folder at the end
        writing = null
        working = true
        ProcessStateFile.write(renamed.dir, ProcessState(id = "record", stage = ProcessStage.ASR))
        store.refresh()
        val processing = store.items.first { !it.single().recordingNow && isProcessing(it.single().state) }.single()
        list.rename(processing, "Другое")
        assertEquals("Другое", store.items.first { it.singleOrNull()?.pendingTitle != null }.single().pendingTitle)
        assertEquals(renamed.dir, card().dir)
        assertEquals("Другое", ProcessStateFile.read(renamed.dir)?.userTitle)
    }

    @Test
    fun movedCardFollowsItsFolderIntoANewSectionAndBack() = runTest {
        val root = tmp.newFolder("Mnema")
        val dir = File(root, "2026-10-21_14-35").apply { mkdirs() }
        WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(1.0, listOf(0.1 to 0.9)))
        Meta.ensureRecording(dir, "record") { Meta.isoNow(1792582500000, TimeZone.getTimeZone("GMT+03:00")) }
        val store = FolderRecordingStore(root, backgroundScope, { null }, { false }, Dispatchers.Unconfined)
        val model = RecordingViewModel(store, Recorded(), root, SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)), io = Dispatchers.Unconfined)
        backgroundScope.launch { model.state.collect {} }
        store.refresh()
        val shown = model.state.first { it is DetailState.Shown } as DetailState.Shown
        assertTrue(DetailAction.MOVE in shown.actions.more)

        model.move("Клиенты: Q4", create = true)

        val moved = model.state.first { (it as? DetailState.Shown)?.detail?.item?.section == "Клиенты Q4" } as DetailState.Shown
        assertEquals("record", moved.detail.item.id)
        assertEquals("2026-10-21_14-35", moved.detail.item.dir.name)
        assertEquals(listOf("Клиенты Q4"), model.sections.value)
        assertFalse(dir.exists())

        model.move("")
        val back = model.state.first { (it as? DetailState.Shown)?.detail?.item?.section == null } as DetailState.Shown
        assertEquals(dir.canonicalFile, back.detail.item.dir.canonicalFile)
        assertEquals("record", back.detail.item.id)
    }

    @Test
    fun aCaseChangeOfItsSectionByTheSyncDoesNotShowTheRecordingAsGone() = runTest {
        val root = tmp.newFolder("Mnema")
        val section = File(root, "grass")
        val dir = recording(section, "2026-10-21_14-35")
        Meta.ensureRecording(dir, "record") { null }
        val store = FolderRecordingStore(root, backgroundScope, { null }, { false }, Dispatchers.Unconfined)
        val model = RecordingViewModel(store, Refusing(), root, SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)),
            io = Dispatchers.Unconfined, clock = testScheduler.timeSource)
        backgroundScope.launch { model.state.collect {} }
        backgroundScope.launch { model.rereading.whileShown() }
        model.state.first { it is DetailState.Shown }

        // between the two renames of changeCase: neither "grass" nor "Grass" is there
        val temp = File(root, ".mnema-rename-Grass")
        assertTrue(section.renameTo(temp))
        advanceTimeBy(5.seconds)
        runCurrent()
        assertTrue(model.state.value is DetailState.Shown)

        assertTrue(temp.renameTo(File(root, "Grass")))
        advanceTimeBy(5.seconds)
        runCurrent()
        val shown = model.state.value as DetailState.Shown
        assertTrue(shown.detail.item.dir.isDirectory)
        assertEquals("record", shown.detail.item.id)
    }

    @Test
    fun stopCancelsProcessingAndRefreshesTheCardWithoutDeletingItsSound() = runTest {
        val root = tmp.newFolder("Mnema")
        val dir = File(root, "2026-10-21_14-35").apply { mkdirs() }
        val audio = File(dir, "mic.wav")
        WavFixture.write(audio, WavFixture.samples(1.0))
        Meta.ensureRecording(dir, "recording") { null }
        ProcessStateFile.write(dir, ProcessState(id = "recording", stage = ProcessStage.ASR))
        val store = FolderRecordingStore(root, backgroundScope, { null }, { true }, Dispatchers.Unconfined)
        val canceled = mutableListOf<File>()
        val scheduler = object : ProcessingScheduler {
            override fun enqueue(dir: File, summaryOnly: Boolean) = false
            override fun reprocess(dir: File, options: ReprocessOptions): File? = null
            override fun cancel(dir: File) {
                canceled += dir
                ProcessStateFile.write(dir, stopped(requireNotNull(ProcessStateFile.read(dir))))
            }
        }
        val model = RecordingViewModel(store, scheduler, root, SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)), io = Dispatchers.Unconfined)
        backgroundScope.launch { model.state.collect {} }
        store.refresh()
        model.state.first { it is DetailState.Shown && it.actions.lead == DetailAction.STOP }

        model.stop()

        model.state.first { it is DetailState.Shown && !isProcessing(it.detail.item.state) }
        assertEquals(listOf(dir), canceled)
        assertTrue(audio.isFile)
        assertFalse(model.deleted.value)
        assertEquals(ProcessStage.STOPPED, ProcessStateFile.read(dir)?.stage)
    }
}
