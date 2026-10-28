package dev.mnema.app.processing

import dev.mnema.app.model.Lang
import dev.mnema.app.transcript.Meta
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class ProcessStateTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private fun dir(name: String = "2026-10-21_14-35"): File = tmp.newFolder(name)

    @Test
    fun stateSurvivesWriteAndRead() {
        val dir = dir()
        val state = ProcessState(
            id = "id-1",
            tracks = mapOf(
                "mic" to TrackState(key = "k", upload = "uploads/u/mic.m4a", state = TrackState.UPLOADED),
                "system" to TrackState(key = "s", uploadId = "ab12", uploaded = 8_388_608),
            ),
            stage = ProcessStage.ASR,
            started = "2026-10-21T14:36:00+03:00",
            node = "https://node", key = "retry", job = "job", sentName = "sent",
            problem = "reason", retries = 1, since = "1:2026-10-21T14:30:00+03:00:job-0",
            options = ReprocessOptions(asr = "groq/whisper-large-v3", tracks = setOf("system")),
            steps = mapOf("prepare_mic" to 18.0, "upload_mic" to 41.5),
        )

        ProcessStateFile.write(dir, state)

        assertEquals(state, ProcessStateFile.read(dir))
        assertEquals(setOf(ProcessStateFile.FILE), dir.list()!!.toSet())
    }

    @Test
    fun missingBrokenOrForeignFileReadsAsNoState() {
        val dir = dir()
        assertNull(ProcessStateFile.read(dir))

        File(dir, ProcessStateFile.FILE).writeText("{\"id\": \"x\", \"tracks\": {")
        assertNull(ProcessStateFile.read(dir))

        File(dir, ProcessStateFile.FILE).writeText("[1, 2]")
        assertNull(ProcessStateFile.read(dir))
    }

    @Test
    fun unknownFieldsAreIgnored() {
        val dir = dir()
        File(dir, ProcessStateFile.FILE).writeText("""{"id":"a","stage":"asr","future":1,"tracks":{"mic":{"key":"k","x":2}}}""")

        val state = ProcessStateFile.read(dir)!!

        assertEquals("a", state.id)
        assertEquals("k", state.tracks.getValue("mic").key)
        assertEquals(TrackState.NEW, state.tracks.getValue("mic").state)
    }

    @Test
    fun newProcessingKeepsIdentityAndDropsPreviousJob() {
        val track = TrackState(key = "k", state = TrackState.UPLOADED)
        val before = ProcessState(
            id = "old",
            tracks = mapOf("mic" to track),
            stage = ProcessStage.DONE,
            started = "2026-10-21T14:36:00+03:00",
            since = SINCE,
        )

        val again = queued(before, summaryOnly = true, id = "old", since = SINCE)

        assertEquals("old", again.id)
        assertTrue(again.tracks.isEmpty())
        assertEquals(ProcessStage.NEW, again.stage)
        assertTrue(again.summaryOnly)
        assertEquals("", again.started)
        assertFalse(queued(again, summaryOnly = false, id = "old", since = SINCE).summaryOnly)
    }

    @Test
    fun reprocessOptionsLiveInTheFileForThisProcessingOnly() {
        val dir = dir()
        val options = ReprocessOptions(lang = Lang.EN, asr = "gigaam", startS = 10.0, endS = 95.0)
        val state = queued(ProcessState(id = "a", stage = ProcessStage.DONE, since = SINCE), false, options, "a", SINCE)

        ProcessStateFile.write(dir, state)

        assertEquals(options, ProcessStateFile.read(dir)!!.options)
        assertNull(queued(state.copy(stage = ProcessStage.DONE), summaryOnly = false, id = "a", since = SINCE).options)
        assertEquals(options, queued(null, summaryOnly = false, options = options, id = "a", since = SINCE).options)
    }

    @Test
    fun audioOnlyIsNotUnfinishedAndStaysWhenStopped() {
        val state = ProcessState(id = "a", stage = ProcessStage.AUDIO_ONLY)

        assertFalse(state.stage in ProcessStage.UNFINISHED)
        assertEquals(ProcessStage.AUDIO_ONLY, stopped(state).stage)
    }

    @Test
    fun folderIsFoundByTheRecordingId() {
        val root = tmp.root
        val first = File(root, "2026-10-21_14-35").apply { mkdirs() }
        val second = File(root, "2026-10-21_Дейли").apply { mkdirs() }
        File(root, "2026-10-20_10-00").mkdirs()
        File(first, "transcript.txt").writeText("mic: один")
        File(second, "transcript.txt").writeText("mic: два")
        Meta.ensureRecording(first, "a") { null }
        Meta.ensureRecording(second, "b") { null }

        assertEquals(second, WorkScheduler.findFolder(root, "b"))
        assertNull(WorkScheduler.findFolder(root, "c"))
        assertNull(WorkScheduler.findFolder(File(root, "нет такой"), "a"))
    }

    @Test fun manualRetryKeepsUnknownSubmissionAndCompletedReprocessStartsFresh() {
        val options = ReprocessOptions(asr = "groq/whisper", tracks = setOf("system"))
        val before = ProcessState("id", stage = ProcessStage.FAILED, key = "same", job = "job", node = "http://node",
            sentName = "before rename", problem = Problems.nodeDown("Сервер"), unreachable = true,
            options = options, tracks = mapOf("system" to TrackState(upload = "u")), retries = 1, since = SINCE)
        val retry = queued(before, false, id = "id", since = SINCE)
        assertEquals(before.copy(stage = ProcessStage.NEW, problem = null, unreachable = false, retries = 0), retry)
        val fresh = queued(before, false, options.copy(lang = Lang.EN), "id", SINCE)
        assertNull(fresh.key)
        assertNull(fresh.job)
        assertNull(fresh.sentName)
        assertTrue(fresh.tracks.isEmpty())
    }

    @Test fun retryAfterAResultWrittenElsewhereStartsFresh() {
        // a failure on the phone, then a successful processing from desktop arrived by sync: the previous job is not continued
        val failed = ProcessState("id", stage = ProcessStage.FAILED, key = "same", job = "job", problem = "Сервер недоступен",
            tracks = mapOf("mic" to TrackState(upload = "u")), since = SINCE)
        val again = queued(failed, false, id = "id", since = "2:2026-10-21T15:00:00+03:00:pc-job")
        assertEquals(ProcessState("id", since = "2:2026-10-21T15:00:00+03:00:pc-job"), again)
        // a work file copied with another folder belongs to someone else
        assertEquals(ProcessState("copy", since = SINCE), queued(failed, false, id = "copy", since = SINCE))
    }

    @Test fun ownTitleIsOneFieldThatRetriesKeepAndTheSheetReplaces() {
        val dir = dir()
        val failed = ProcessState("id", stage = ProcessStage.FAILED, key = "same", userTitle = "Планёрка", since = SINCE)
        ProcessStateFile.write(dir, failed)
        assertEquals("Планёрка", ProcessStateFile.read(dir)!!.userTitle)

        assertEquals("Планёрка", queued(failed, false, id = "id", since = SINCE).userTitle)
        assertEquals("Планёрка", queued(failed.copy(stage = ProcessStage.STOPPED), false, id = "id", since = SINCE).userTitle)
        // the processing window: the own title is whatever is left in it
        val sheet = queued(failed, false, ReprocessOptions(lang = Lang.EN, title = "Дейли"), "id", SINCE)
        assertEquals("Дейли" to null, sheet.userTitle to sheet.options!!.title)
        assertNull(queued(failed, false, ReprocessOptions(lang = Lang.EN), "id", SINCE).userTitle)
        assertNull(queued(failed, false, ReprocessOptions(autoName = true), "id", SINCE).userTitle)
        assertEquals("Дейли", queued(null, false, ReprocessOptions(title = "Дейли"), "id", SINCE).userTitle)
    }

    @Test fun stoppedProcessingDropsItsOwnTitleAsOnTheDesktop() {
        assertNull(stopped(ProcessState("id", stage = ProcessStage.ASR, job = "j1", userTitle = "Планёрка")).userTitle)
    }

    private companion object {
        const val SINCE = "1:2026-10-21T14:40:00+03:00:job-0"
    }
}
