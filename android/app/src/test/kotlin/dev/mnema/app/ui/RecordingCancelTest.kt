package dev.mnema.app.ui

import dev.mnema.app.helper.HelperStatus
import dev.mnema.app.recordings.FolderRecordingStore
import dev.mnema.app.transcript.Meta
import dev.mnema.app.transcript.WavFixture
import dev.mnema.app.ui.record.CancelMarks
import dev.mnema.app.ui.record.CancelOutcome
import dev.mnema.app.ui.record.CancelSteps
import dev.mnema.app.ui.record.cancelRecording
import java.io.File
import java.io.IOException
import java.util.TimeZone
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeFalse
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class RecordingCancelTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val root: File get() = File(tmp.root, "Mnema").apply { mkdirs() }
    private val name = "2026-10-23_10-05"
    private var status: HelperStatus? = null
    private val trashed = mutableListOf<String>()
    private var stops = 0
    private val marks = CancelMarks()

    private fun recordingInto(folder: String) = HelperStatus(v = 1, state = "recording", folder = folder, startedMs = 1)

    private fun stoppedAfter(folder: String) = HelperStatus(v = 1, state = "idle", lastFolder = folder)

    private fun recording(folder: String, id: String = "id-$folder"): File {
        val dir = File(root, folder).apply { mkdirs() }
        WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(1.0))
        Meta.ensureRecording(dir, id) { Meta.isoNow(1_000, TimeZone.getTimeZone("UTC")) }
        return dir
    }

    private fun store(trash: Boolean = true, trashFile: (File) -> Unit = { trashed += it.name; assertTrue(it.renameTo(File(tmp.root, "trashed-${it.name}"))) }) =
        FolderRecordingStore(
            root, CoroutineScope(Dispatchers.Unconfined), { status?.takeIf { it.recording }?.folder }, { false }, Dispatchers.Unconfined,
            trash = trashFile, hasTrash = { trash },
        )

    // the helper stops on stop; [then] is what it does right after
    private fun steps(store: FolderRecordingStore = store(), then: () -> Unit = { status = stoppedAfter(name) }) =
        CancelSteps(root, { status }, { stops++; then() }, store)

    private fun cancel(steps: CancelSteps, folder: String = name) = runBlocking { cancelRecording(folder, steps, marks, waitMs = 100, stepMs = 5) }

    @Test
    fun cancelStopsTheRecordingAndTrashesOnlyItsFolder() {
        val earlier = recording("2026-10-23_09-00")
        val dir = recording(name)
        status = recordingInto(name)

        assertEquals(CancelOutcome.Deleted, cancel(steps()))

        assertEquals(1, stops)
        assertEquals(listOf(name), trashed)
        assertFalse(dir.exists())
        assertTrue(earlier.exists())
    }

    @Test
    fun recordingStartedRightAfterTheStopIsKept() {
        val dir = recording(name)
        val next = "2026-10-23_10-06"
        status = recordingInto(name)

        val outcome = cancel(steps(then = { recording(next); status = recordingInto(next) }))

        assertEquals(CancelOutcome.Deleted, outcome)
        assertEquals(listOf(name), trashed)
        assertFalse(dir.exists())
        assertTrue(File(root, next).exists())
        assertFalse(marks.cancelled(File(root, next)))
    }

    @Test
    fun withoutTrashTheFolderIsDeletedForever() {
        val dir = recording(name)
        status = recordingInto(name)

        assertEquals(CancelOutcome.Deleted, cancel(steps(store(trash = false))))

        assertFalse(dir.exists())
        assertEquals(emptyList<String>(), trashed)
    }

    @Test
    fun recordingThatIsNoLongerWrittenIsLeftAlone() {
        val dir = recording(name)
        val other = recording("2026-10-23_10-06")

        status = stoppedAfter(name)
        assertEquals("остановлена раньше отмены: её уже обрабатывают", CancelOutcome.NotRecording, cancel(steps()))
        status = recordingInto(other.name)
        assertEquals("пишется уже другая", CancelOutcome.NotRecording, cancel(steps()))

        assertEquals(0, stops)
        assertTrue(dir.exists() && other.exists())
        assertFalse(marks.cancelled(dir))
        assertFalse(marks.cancelled(other))
    }

    @Test
    fun unreadableHeartbeatStopsNothing() {
        val dir = recording(name)
        status = null

        assertEquals(CancelOutcome.NotSent, cancel(steps()))

        assertEquals(0, stops)
        assertTrue(dir.exists())
        assertFalse(marks.cancelled(dir))
    }

    @Test
    fun stopThatWasNotSentKeepsTheRecordingWithoutAMark() {
        val dir = recording(name)
        status = recordingInto(name)

        assertEquals(CancelOutcome.NotSent, cancel(steps(then = { throw IOException("нет папки обмена") })))

        assertTrue(dir.exists())
        assertFalse("обычная остановка потом обработает запись", marks.cancelled(dir))
        assertFalse(CancelMarks().cancelled(dir))
    }

    @Test
    fun recordingThatWasNotDeletedStaysCancelledEvenAfterRestart() {
        val dir = recording(name)
        status = recordingInto(name)
        val failing = store(trashFile = { throw IOException("Система не переместила запись в корзину") })

        assertEquals(CancelOutcome.NotDeleted("Система не переместила запись в корзину"), cancel(steps(failing)))

        assertTrue(dir.exists())
        assertTrue(marks.cancelled(dir))
        assertTrue("метка в папке", CancelMarks().cancelled(File(root.path, name)))
    }

    @Test
    fun helperThatKeepsWritingKeepsItsFolder() {
        val dir = recording(name)
        status = recordingInto(name)

        val outcome = cancel(steps(then = {}))

        assertTrue(outcome is CancelOutcome.NotDeleted)
        assertTrue(dir.exists())
        assertTrue(File(dir, "mic.wav").exists())
        assertTrue(marks.cancelled(dir))
    }

    @Test
    fun anotherRecordingUnderTheSameNameIsNotDeleted() {
        val dir = recording(name, id = "отменённая")
        status = recordingInto(name)

        val outcome = cancel(steps(then = {
            File(dir, Meta.FILE).writeText("{\"version\": 1, \"id\": \"другая\"}")
            status = stoppedAfter(name)
        }))

        assertEquals(CancelOutcome.NotDeleted(null), outcome)
        assertTrue(dir.exists())
        assertEquals(emptyList<String>(), trashed)
    }

    @Test
    fun markInMemoryCoversOnlyTheSameRecording() {
        val dir = recording(name, id = "отменённая")
        dir.setWritable(false)
        try {
            assumeFalse("метку в папке не создать", dir.canWrite())
            marks.mark(dir, "отменённая")
            assertTrue(marks.cancelled(dir))
        } finally {
            dir.setWritable(true)
        }
        File(dir, Meta.FILE).writeText("{\"version\": 1, \"id\": \"другая\"}")

        assertFalse(marks.cancelled(dir))
    }
}
