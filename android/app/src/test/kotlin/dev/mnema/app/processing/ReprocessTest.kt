package dev.mnema.app.processing

import dev.mnema.app.model.Lang
import dev.mnema.app.recordings.outcomeKey
import dev.mnema.app.transcript.RecordingFolder
import dev.mnema.app.transcript.Wav
import dev.mnema.app.transcript.WavFixture
import dev.mnema.app.transcript.Meta
import java.util.TimeZone
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class ReprocessTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val root: File get() = File(tmp.root, "Mnema").apply { mkdirs() }

    private fun recording(name: String = "2026-10-21_14-35", seconds: Double = 100.0): File {
        val dir = File(root, name).apply { mkdirs() }
        WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(seconds, loud = listOf(5.0 to seconds - 5)))
        WavFixture.write(File(dir, "system.wav"), WavFixture.samples(seconds - 2, loud = listOf(10.0 to 20.0)))
        File(dir, "mic.wav").setLastModified(1_790_000_000_000L)
        return dir
    }

    // the recording's own work: id — the recording id in meta, the mark — its history now
    private fun withWork(dir: File, state: ProcessState): ProcessState {
        Meta.ensureRecording(dir, state.id) { null }
        return state.copy(since = outcomeKey(Meta.read(dir))).also { ProcessStateFile.write(dir, it) }
    }

    // name, bytes and time of each file: the source does not change after cutting
    private fun snapshot(dir: File): Map<String, Pair<Int, Long>> =
        dir.listFiles()!!.associate { it.name to (it.readBytes().contentHashCode() to it.lastModified()) }

    @Test
    fun partHasItsOwnIdAndTheOriginalStartWithoutHistory() {
        val dir = recording()
        Meta.ensureRecording(dir, "original") { Meta.isoNow(1_760_000_000_000, TimeZone.getTimeZone("Europe/Moscow")) }
        val before = snapshot(dir)
        val part = reprocessed(dir, root, ReprocessOptions(startS = 10.0, endS = 30.0)) { "part" }!!
        val data = Meta.read(part)!!
        // the work of a part — by the part id in meta
        assertEquals("part", ProcessStateFile.own(part)?.id)
        assertEquals(JsonPrimitive("part"), data["id"])
        assertNotEquals(Meta.read(dir)?.get("id"), data["id"])
        assertEquals(Meta.started(dir), Meta.started(part))
        assertTrue(data["runs"]!!.jsonArray.isEmpty())
        assertEquals(before, snapshot(dir))
    }

    @Test
    fun partIsCutIntoASiblingAndTheOriginalStaysIntact() {
        val dir = recording()
        val before = snapshot(dir)

        val part = sliceRecording(dir, 10.0, 95.0)

        assertEquals(File(root, "2026-10-21_14-35 (0.00.10–0.01.35)"), part)
        for (name in listOf("mic.wav", "system.wav")) {
            assertEquals(name, 85.0, Wav.seconds(File(part, name)), 0.1)
            assertEquals(name, File(dir, name).lastModified(), File(part, name).lastModified())
        }
        assertEquals(setOf("mic.wav", "system.wav", ".mnema-meta.json"), part.list()!!.toSet())
        assertEquals(before, snapshot(dir))
    }

    @Test
    fun theSamePartAgainGetsItsOwnFolder() {
        val dir = recording()

        val first = sliceRecording(dir, 10.0, 95.0)
        val second = sliceRecording(dir, 10.0, 95.0)

        assertEquals("2026-10-21_14-35 (0.00.10–0.01.35) (2)", second.name)
        assertTrue(first.isDirectory)
    }

    @Test
    fun partOutsideTheRecordingIsRefusedWithoutAFolder() {
        val dir = recording()

        assertThrows(IllegalArgumentException::class.java) { sliceRecording(dir, 100.0, 120.0) }
        assertThrows(IllegalArgumentException::class.java) { sliceRecording(dir, 50.0, 40.0) }

        assertEquals(listOf(dir.name), root.list()!!.toList())
    }

    @Test
    fun reprocessingAPartQueuesTheSiblingWithItsOptions() {
        val dir = recording()

        val part = reprocessed(dir, root, ReprocessOptions(lang = Lang.EN, startS = 10.0, endS = 200.0)) { "part-id" }!!

        assertEquals("2026-10-21_14-35 (0.00.10–0.01.40)", part.name)
        val state = ProcessStateFile.read(part)!!
        assertEquals("part-id", state.id)
        assertEquals(ProcessStage.NEW, state.stage)
        assertEquals(ReprocessOptions(lang = Lang.EN, startS = 10.0, endS = 100.0), state.options)
        assertNull(ProcessStateFile.read(dir))
    }

    @Test
    fun wholeRangeIsProcessedInPlaceKeepingTheId() {
        val dir = recording()
        val done = TrackState(key = "k", state = TrackState.UPLOADED)
        withWork(dir, ProcessState(id = "old", tracks = mapOf("mic" to done), stage = ProcessStage.DONE))

        val same = reprocessed(dir, root, ReprocessOptions(asr = "whisper", startS = 0.4, endS = 99.5)) { "new" }

        assertEquals(dir, same)
        val state = ProcessStateFile.read(dir)!!
        assertEquals("old", state.id)
        assertEquals(ProcessStage.NEW, state.stage)
        assertTrue(state.tracks.isEmpty())
        assertEquals(ReprocessOptions(asr = "whisper"), state.options)
        assertEquals(listOf(dir.name), root.list()!!.toList())
    }

    @Test
    fun wholeReprocessOfAPartKeepsItsPlaceInTheOriginal() {
        val part = reprocessed(recording(), root, ReprocessOptions(startS = 10.0, endS = 30.0)) { "part" }!!

        assertEquals(part, reprocessed(part, root, ReprocessOptions(lang = Lang.EN)) { "unused" })

        assertEquals(ReprocessOptions(lang = Lang.EN, startS = 10.0, endS = 30.0), ProcessStateFile.read(part)!!.options)
    }

    @Test
    fun changedRunCancelsTheFailedServerJobInsteadOfLeavingItQueued() {
        val dir = recording()
        val failed = ProcessState(id = "run", stage = ProcessStage.FAILED, job = "job-1", key = "request", node = "http://node",
            problem = "Mac mini недоступен", unreachable = true, options = ReprocessOptions(lang = Lang.EN, tracks = setOf("mic")),
            tracks = mapOf("mic" to TrackState(key = "k", upload = "uploads/u1/mic.m4a", state = TrackState.UPLOADED)))
            .let { withWork(dir, it) }
        val cancelled = mutableListOf<ProcessState>()

        val (target, state) = replaceProcessing(root, dir, ReprocessOptions(lang = Lang.RU), cancelled::add) { "unused" }!!

        assertEquals(dir, target)
        assertEquals(listOf(failed), cancelled)
        assertEquals("run", state.id)
        assertEquals(ProcessStage.NEW, state.stage)
        assertNull(state.job)
        assertNull(state.key)
        assertTrue(state.tracks.isEmpty())
        assertEquals(ReprocessOptions(lang = Lang.RU), state.options)
        assertEquals(state, ProcessStateFile.read(dir))
    }

    @Test
    fun partFromAFailedRunCancelsItsJobAndTheOriginalNoLongerWaitsForIt() {
        val dir = recording()
        val failed = withWork(dir, ProcessState(id = "run", stage = ProcessStage.FAILED, job = "job-1", key = "request", node = "http://node", problem = "Сбой"))
        val cancelled = mutableListOf<ProcessState>()

        val (part, state) = replaceProcessing(root, dir, ReprocessOptions(startS = 10.0, endS = 30.0), cancelled::add) { "part" }!!

        assertNotEquals(dir, part)
        assertEquals("part", state.id)
        assertEquals(listOf(failed), cancelled)
        val original = ProcessStateFile.read(dir)!!
        assertEquals(ProcessStage.FAILED, original.stage)
        assertEquals("Сбой", original.problem)
        assertNull(original.job)
    }

    @Test
    fun finishedOrCopiedJobIsNotCancelled() {
        val done = recording()
        withWork(done, ProcessState(id = "done", stage = ProcessStage.DONE, job = "forgotten", node = "http://node"))
        val first = recording("2026-10-21_15-00")
        val copy = recording("2026-10-21_15-00 копия")
        val shared = withWork(first, ProcessState(id = "shared", stage = ProcessStage.FAILED, job = "job-1", key = "request", node = "http://node"))
        // a folder copy with a work file: the copy has its own recording id
        ProcessStateFile.write(copy, shared)
        val cancelled = mutableListOf<ProcessState>()

        replaceProcessing(root, done, ReprocessOptions(lang = Lang.EN), cancelled::add) { "unused" }!!
        val (_, fresh) = replaceProcessing(root, copy, ReprocessOptions(lang = Lang.EN), cancelled::add) { "copy" }!!

        assertTrue(cancelled.isEmpty())
        assertEquals("copy", fresh.id)
        assertEquals(shared, ProcessStateFile.read(first))
    }

    @Test
    fun folderWithoutATrackOrOutsideTheRootIsRefused() {
        val textOnly = File(root, "2026-10-21_Дейли").apply { mkdirs() }
        File(textOnly, RecordingFolder.TRANSCRIPT).writeText("mic: да\n")
        val outside = File(tmp.root, "2026-10-21_14-35").apply { mkdirs() }
        WavFixture.write(File(outside, "mic.wav"), WavFixture.samples(1.0))

        assertNull(reprocessed(textOnly, root, ReprocessOptions()) { "x" })
        assertNull(reprocessed(outside, root, ReprocessOptions(startS = 0.2, endS = 0.5)) { "x" })
        assertNull(reprocessed(recording(), root, ReprocessOptions(startS = 120.0)) { "x" })
        assertEquals(setOf("2026-10-21_Дейли", "2026-10-21_14-35"), root.list()!!.toSet())
    }
}
