package dev.mnema.app.recordings

import dev.mnema.app.processing.ProcessStage
import dev.mnema.app.processing.ProcessState
import dev.mnema.app.processing.ProcessStateFile
import dev.mnema.app.transcript.Meta
import dev.mnema.app.transcript.WavFixture
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.util.TimeZone
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class RecordingRenameTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val root: File get() = File(tmp.root, "Mnema").apply { mkdirs() }
    private var recordingNow: String? = null
    private var working: (String) -> Boolean = { false }

    private fun store() = FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), { recordingNow }, { working(it) }, Dispatchers.Unconfined)

    // a phone recording: the start in meta with the recording zone offset
    private fun recording(name: String, id: String = "record-$name"): File = File(root, name).apply {
        mkdirs()
        WavFixture.write(File(this, "mic.wav"), WavFixture.samples(1.0, listOf(0.1 to 0.9)))
        Meta.ensureRecording(this, id) { Meta.isoNow(1792560920000, TimeZone.getTimeZone("GMT+03:00")) }
    }

    @Test
    fun renameKeepsIdentityAndNeverTakesAnotherFolder() = runBlocking {
        val dir = recording("2026-10-21_08-35")
        val other = recording("2026-10-21_Дейли: API".replace(":", ""), id = "other")
        File(other, "transcript.txt").writeText("mic: чужая\n")
        val store = store()

        val renamed = store.rename(dir, "Дейли: API")!!

        assertEquals("2026-10-21_Дейли API (2)", renamed.name)
        assertFalse(dir.exists())
        assertEquals("record-2026-10-21_08-35", recordingId(renamed))
        assertEquals("Дейли API", store.detail(renamed)!!.item.title)
        assertEquals("mic: чужая\n", File(other, "transcript.txt").readText())
        assertEquals("other", recordingId(other))
        assertEquals(renamed.name, findRecording(root, "record-2026-10-21_08-35")?.name)
    }

    @Test
    fun emptyTitleReturnsTheNameByStartTime() = runBlocking {
        val dir = recording("2026-10-21_Дейли")
        ProcessStateFile.write(dir, ProcessState("run", stage = ProcessStage.DONE))
        val store = store()

        val renamed = store.rename(dir, "  ")!!

        assertEquals("2026-10-21_08-35", renamed.name)
        val item = store.detail(renamed)!!.item
        assertTrue(item.timeTitle)
        assertTrue(item.title, item.title.endsWith(":35"))
    }

    @Test
    fun recordingFolderIsNotRenamedAndAProcessedOneWaitsForTheEndOfProcessing() = runBlocking {
        val writing = recording("2026-10-21_08-35")
        recordingNow = writing.name
        val store = store()
        assertNull(store.rename(writing, "Дейли"))
        assertTrue(writing.isDirectory)
        assertEquals("Запись занята; дождитесь завершения", store.failure.value)

        recordingNow = null
        working = { true }
        val processing = recording("2026-10-21_09-00")
        val run = recordingId(processing)!!
        ProcessStateFile.write(processing, ProcessState(run, stage = ProcessStage.ASR))
        assertEquals(processing, store.rename(processing, "Дейли: API"))
        assertTrue(processing.isDirectory)
        assertEquals("Дейли: API", ProcessStateFile.read(processing)!!.userTitle)
        // the title is still the folder name; the set one is a caption until processing ends
        val item = store.detail(processing)!!.item
        assertTrue(item.timeTitle)
        assertEquals("Дейли: API", item.pendingTitle)
        // empty clears the set title, so the server names it
        assertEquals(processing, store.rename(processing, "  "))
        assertNull(ProcessStateFile.read(processing)!!.userTitle)
        assertNull(store.detail(processing)!!.item.pendingTitle)
        // no work for the file: the recording is shown by its files, without a pending title
        ProcessStateFile.write(processing, ProcessState(run, stage = ProcessStage.ASR, userTitle = "Дейли"))
        working = { false }
        assertNull(store.detail(processing)!!.item.pendingTitle)

        working = { true }
        ProcessStateFile.write(processing, ProcessState(run, stage = ProcessStage.DONE))
        assertNull(store.rename(processing, "Дейли"))
        assertTrue(processing.isDirectory)
        assertNull(ProcessStateFile.read(processing)!!.userTitle)
    }

    @Test
    fun renameAfterProcessingReplacesTheTitleItWaitedFor() = runBlocking {
        val part = recording("2026-10-21_08-35 (0.10–1.35)")
        ProcessStateFile.write(part, ProcessState("run", stage = ProcessStage.FAILED, userTitle = "Дейли"))
        val store = store()
        val failed = store.detail(part)!!.item
        assertTrue(failed.timeTitle)
        assertNull("a failed run will not name the folder", failed.pendingTitle)

        val renamed = store.rename(part, "Планёрка (0.10–1.35)")!!

        assertEquals("2026-10-21_Планёрка (0.10–1.35)", renamed.name)
        assertNull(ProcessStateFile.read(renamed)!!.userTitle)
        assertEquals("Планёрка (0.10–1.35)", store.detail(renamed)!!.item.title)
    }

    /** A case-only rename on shared storage is two real renames: MediaProvider does not carry a direct one over to the helper's files. */
    @Test
    fun caseOnlyRenameNeverRenamesByCaseAlone() {
        val dir = recording("2026-10-21_Дейли")
        val target = File(root, "2026-10-21_дейли")
        val moves = mutableListOf<Pair<String, String>>()

        changeCase(dir, target) { a, b -> moves += a.name to b.name; Files.move(a.toPath(), b.toPath()) }

        assertEquals(listOf("2026-10-21_дейли"), root.list()!!.toList())
        assertEquals("record-2026-10-21_Дейли", recordingId(target))
        assertEquals(2, moves.size)
        assertTrue(moves.none { (a, b) -> a.equals(b, ignoreCase = true) })
    }

    @Test
    fun failedSecondStepOfACaseOnlyRenameReturnsTheOriginalName() {
        val dir = recording("2026-10-21_Дейли")
        val target = File(root, "2026-10-21_дейли")

        val error = assertThrows(IOException::class.java) {
            changeCase(dir, target) { a, b -> if (b == target) throw IOException("занято") else Files.move(a.toPath(), b.toPath()) }
        }

        assertEquals("занято", error.message)
        assertEquals(listOf("2026-10-21_Дейли"), root.list()!!.toList())
    }

    @Test
    fun leftoverTemporaryFolderIsRestoredBeforeTheListIsShown() {
        val dir = recording("2026-10-21_Дейли")
        val target = File(root, "2026-10-21_дейли")
        assertThrows(IOException::class.java) {
            changeCase(dir, target) { a, b ->
                if (b == target || b == dir) throw IOException("сбой") else Files.move(a.toPath(), b.toPath())
            }
        }
        assertEquals(listOf(".mnema-rename-2026-10-21_дейли"), root.list()!!.toList())

        val store = store()
        store.refresh()

        assertEquals(listOf("2026-10-21_дейли"), store.items.value.map { it.dir.name })
        assertEquals("record-2026-10-21_Дейли", store.items.value.single().id)
        assertEquals(listOf("2026-10-21_дейли"), root.list()!!.toList())
    }

}
