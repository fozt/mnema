package dev.mnema.app.processing

import dev.mnema.app.recordings.RecordState
import dev.mnema.app.recordings.Work
import dev.mnema.app.recordings.busyRecordings
import dev.mnema.app.recordings.recordingId
import dev.mnema.app.recordings.outcomeKey
import dev.mnema.app.recordings.recordState
import dev.mnema.app.sync.renewIdentity
import dev.mnema.app.transcript.Meta
import dev.mnema.app.transcript.RecordingFolder
import dev.mnema.app.transcript.WavFixture
import java.io.File
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class WorkSchedulerTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val root: File get() = File(tmp.root, "Mnema").apply { mkdirs() }

    private fun folder(name: String, wav: Boolean = true, transcript: String? = null): File {
        val dir = File(root, name).apply { mkdirs() }
        if (wav) WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(1.0))
        transcript?.let { File(dir, RecordingFolder.TRANSCRIPT).writeText(it) }
        return dir
    }

    // the recording's own work: id — the recording id in meta, the mark — its history now
    private fun work(dir: File, id: String, stage: String, job: String? = null): ProcessState {
        Meta.ensureRecording(dir, id) { null }
        return ProcessState(id = id, stage = stage, job = job, key = job?.let { "request" }, node = "node",
            since = outcomeKey(Meta.read(dir))).also { ProcessStateFile.write(dir, it) }
    }

    @Test
    fun outsideAndNestedRecordingFoldersAreRefused() {
        val outside = File(tmp.root, "Чужая").apply { mkdirs() }
        File(outside, RecordingFolder.TRANSCRIPT).writeText("mic: да\n")
        val record = folder("2026-10-21_14-35").apply { File(this, ".mnema-meta.json").writeText("{}") }
        val nested = File(record, "внутри").apply { mkdirs() }
        File(nested, RecordingFolder.TRANSCRIPT).writeText("mic: да\n")
        val hidden = folder(".скрытая", wav = false, transcript = "mic: да\n")

        for (dir in listOf(outside, nested, hidden, File(root, "нет такой"), root)) {
            assertFalse(dir.path, WorkScheduler.accepts(dir, root, summaryOnly = true))
        }
    }

    @Test
    fun unfinishedFoldersWithoutWorkAreOrphaned() {
        val stages = listOf(ProcessStage.NEW, ProcessStage.ASR, ProcessStage.SUMMARY)
        val unfinished = stages.mapIndexed { i, stage -> folder("2026-10-21_10-0$i").also { work(it, "u$i", stage) } }
        val running = folder("2026-10-21_11-00").also { work(it, "run", ProcessStage.ASR) }
        listOf(ProcessStage.DONE, ProcessStage.FAILED, ProcessStage.STOPPED).forEachIndexed { i, stage ->
            work(folder("2026-10-21_12-0$i"), "f$i", stage)
        }
        folder("2026-10-21_13-00")
        // an outcome was added without this work (sync with desktop) — it is not continued
        folder("2026-10-21_14-00").also { work(it, "late", ProcessStage.ASR) }
            .also { Meta.appendRun(it, Json.parseToJsonElement("""{"status":"ok","job":"pc"}""").jsonObject) }
        // a work file of another recording (copied with the folder) belongs to someone else
        folder("2026-10-21_15-00").also { ProcessStateFile.write(it, ProcessState(id = "other", stage = ProcessStage.ASR)) }

        val orphaned = WorkScheduler.orphaned(root) { id -> id == "run" }

        assertEquals(unfinished.toSet(), orphaned.toSet())
        assertFalse(running in orphaned)
        assertEquals(emptyList<File>(), WorkScheduler.orphaned(File(root, "нет такой")) { false })
    }

    @Test
    fun stoppingDropsSharedJobAndKeepsReusableUploads() {
        val uploaded = TrackState(key = "k1", upload = "uploads/u1/mic.m4a", state = TrackState.UPLOADED)
        val state = ProcessState(id = "a", tracks = mapOf("mic" to uploaded), job = "job-1", key = "request", stage = ProcessStage.ASR)
        val stopped = stopped(state)
        assertEquals(ProcessStage.STOPPED, stopped.stage)
        assertEquals(uploaded, stopped.tracks.getValue("mic"))
        assertNull(stopped.job)
        assertEquals(ProcessStage.DONE, stopped(state.copy(stage = ProcessStage.DONE)).stage)
    }
    @Test
    fun stoppingWritesTheStoppedProcessingToTheHistory() {
        val dir = folder("2026-10-21_10-00")
        val state = work(dir, "a", ProcessStage.ASR, job = "remote-job")
        val cancelled = mutableListOf<ProcessState>()

        assertTrue(stopProcessing(root, dir, cancelled::add))

        assertEquals(listOf(state), cancelled)
        val sizes = dir.listFiles()!!.associate { it.name to it.length() }
        assertEquals(RecordState.AUDIO_ONLY, recordState(sizes, Meta.read(dir), Work.NONE))
        assertTrue(WorkScheduler.orphaned(root) { false }.isEmpty())
    }

    @Test
    fun stoppingASummaryLeavesTheHistoryAsItWas() {
        val dir = folder("2026-10-21_10-00", transcript = "mic: да\n")
        Meta.ensureRecording(dir, "a") { null }
        Meta.appendRun(dir, Json.parseToJsonElement("""{"status":"ok","job":"first"}""").jsonObject)
        ProcessStateFile.write(dir, work(dir, "a", ProcessStage.SUMMARY, job = "remote-job").copy(summaryOnly = true))
        val history = Meta.read(dir)
        val cancelled = mutableListOf<ProcessState>()

        assertTrue(stopProcessing(root, dir, cancelled::add))

        assertEquals(listOf("remote-job"), cancelled.map { it.job })
        assertEquals(history, Meta.read(dir))
        val sizes = dir.listFiles()!!.associate { it.name to it.length() }
        assertEquals(RecordState.PROCESSED, recordState(sizes, Meta.read(dir), Work.NONE))
    }

    @Test
    fun workOvertakenByAResultFromAnotherDeviceDoesNotKeepTheRecordBusy() {
        val dir = folder("2026-10-21_10-00", transcript = "mic: да\n")
        work(dir, "a", ProcessStage.ASR, job = "remote-job")
        assertTrue("a" in busyRecordings(root, null, { false }))

        Meta.appendRun(dir, Json.parseToJsonElement("""{"status":"ok","job":"pc"}""").jsonObject)

        assertFalse("a" in busyRecordings(root, null, { false }))
        assertTrue("a" in busyRecordings(root, null, { it == "a" }))
    }

    @Test
    fun withSyncTheWorkStaysWithTheFolderTheSyncKeepsTheIdFor() {
        val first = folder("Original")
        work(first, "shared", ProcessStage.ASR, job = "remote-job")
        Thread.sleep(10)
        // the original was moved into a section and a copy was put in its old place
        val moved = File(root, "Раздел/Original").also { File(root, "Раздел").mkdirs(); assertTrue(first.renameTo(it)) }
        val placed = File(root, "Original").also { moved.copyRecursively(it) }

        assertEquals(placed, WorkScheduler.findFolder(root, "shared", syncing = true, previous = "Original"))
        assertEquals(listOf("shared", "shared"), listOf(recordingId(moved), recordingId(placed)))
        assertEquals(moved, WorkScheduler.findFolder(root, "shared"))
        assertEquals(null, ProcessStateFile.own(placed))
    }

    @Test
    fun copiedFolderLeavesTheOriginalWorkAndItsServerJobAlone() {
        val first = folder("Original")
        val state = work(first, "shared", ProcessStage.ASR, job = "remote-job")
        val copy = File(root, "Copy").also { first.copyRecursively(it) }
        renewIdentity(copy)
        val cancelled = mutableListOf<ProcessState>()

        assertFalse(stopProcessing(root, copy, cancelled::add))

        assertTrue(cancelled.isEmpty())
        assertEquals(state, ProcessStateFile.read(first))
        assertEquals(listOf(first), WorkScheduler.orphaned(root) { false })
    }

    @Test
    fun aCopyMadeMidProcessingGetsItsOwnIdAndTheOriginalKeepsTheWork() {
        val first = folder("Original")
        work(first, "shared", ProcessStage.ASR, job = "remote-job")
        Thread.sleep(10)
        val copy = File(root, "Copy").also { first.copyRecursively(it) }

        assertEquals(first, WorkScheduler.findFolder(root, "shared"))
        assertEquals("shared", ProcessStateFile.own(first)?.id)
        assertEquals(null, ProcessStateFile.own(copy))
        assertEquals(listOf(first), WorkScheduler.orphaned(root) { false })
    }
}
