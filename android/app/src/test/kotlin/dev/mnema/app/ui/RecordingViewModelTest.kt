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
import dev.mnema.app.transcript.WavFixture
import java.util.TimeZone
import dev.mnema.app.ui.recordings.DetailState
import dev.mnema.app.ui.recordings.DetailAction
import dev.mnema.app.ui.recordings.isProcessing
import dev.mnema.app.ui.recordings.RecordingViewModel
import java.io.File
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.UnconfinedTestDispatcher
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
        val store = FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), { null }, { false }, Dispatchers.Unconfined)
        val scheduler = Refusing()
        val model = RecordingViewModel(store, scheduler, root, SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)))
        backgroundScope.launch { model.state.collect {} }
        store.refresh()
        model.state.first { it is DetailState.Shown }

        model.retry()

        assertEquals("Запись нельзя обработать: нет звука", model.failure.first { it != null })
        assertEquals(listOf(false), scheduler.asked)
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
        val store = FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), { null }, { false }, Dispatchers.Unconfined)
        val scheduler = Recorded()
        val model = RecordingViewModel(store, scheduler, root, SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)))
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
        val store = FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), { null }, { working }, Dispatchers.Unconfined)
        val model = RecordingViewModel(store, Recorded(), root, SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)))
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
    fun movedCardFollowsItsFolderIntoANewSectionAndBack() = runTest {
        val root = tmp.newFolder("Mnema")
        val dir = File(root, "2026-10-21_14-35").apply { mkdirs() }
        WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(1.0, listOf(0.1 to 0.9)))
        Meta.ensureRecording(dir, "record") { Meta.isoNow(1792582500000, TimeZone.getTimeZone("GMT+03:00")) }
        val store = FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), { null }, { false }, Dispatchers.Unconfined)
        val model = RecordingViewModel(store, Recorded(), root, SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)))
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
        val model = RecordingViewModel(store, scheduler, root, SavedStateHandle(mapOf(RecordingViewModel.FOLDER_ARG to dir.path)))
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
