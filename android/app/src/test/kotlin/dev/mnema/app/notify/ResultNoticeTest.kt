package dev.mnema.app.notify

import dev.mnema.app.processing.ProcessState
import dev.mnema.app.processing.ProcessStateFile
import java.io.File
import dev.mnema.app.transcript.Meta
import dev.mnema.app.ui.recordings.FolderFollower
import java.util.TimeZone
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class ResultNoticeTest {
    @get:Rule
    val tmp = TemporaryFolder()

    @Test
    fun resultOfRenamedRecordingReplacesTheEarlierOne() {
        val before = tmp.newFolder("2026-10-22_14-05")
        ProcessStateFile.write(before, ProcessState(id = "different-process-id"))
        Meta.ensureRecording(before, "6f1c2a4e-0b7d-4c1e-9a3f-5d2e8b7c1a90") { Meta.isoNow(1000L, TimeZone.getTimeZone("UTC")) }
        val failed = ResultNotice.of(before)
        val after = File(tmp.root, "Работа/2026-10-22_Дейли").apply { parentFile!!.mkdirs() }
        check(before.renameTo(after))

        val done = ResultNotice.of(after)

        assertEquals(failed.tag, done.tag)
        assertEquals("6f1c2a4e-0b7d-4c1e-9a3f-5d2e8b7c1a90", done.id)
        assertEquals(before.absolutePath, failed.folder)
        assertEquals(after.absolutePath, done.folder)
        assertEquals(after, FolderFollower(tmp.root, File(failed.folder), recording = failed.id).current())
        before.mkdir()
        Meta.ensureRecording(before, "replacement") { Meta.isoNow(1000L, TimeZone.getTimeZone("UTC")) }
        assertEquals(after, FolderFollower(tmp.root, File(failed.folder), recording = failed.id).current())
        after.deleteRecursively()
        assertNull(FolderFollower(tmp.root, File(failed.folder), recording = failed.id).current())
    }

    @Test
    fun differentRecordingsKeepTheirOwnResults() {
        val first = tmp.newFolder("2026-10-22_14-05").also { Meta.ensureRecording(it, "first") { Meta.isoNow(1000L, TimeZone.getTimeZone("UTC")) } }
        val second = tmp.newFolder("2026-10-22_15-30").also { Meta.ensureRecording(it, "second") { Meta.isoNow(1000L, TimeZone.getTimeZone("UTC")) } }

        assertNotEquals(ResultNotice.of(first).tag, ResultNotice.of(second).tag)
    }

    @Test
    fun recordingWithoutMetadataDoesNotBorrowProcessingIdentity() {
        val dir = tmp.newFolder("2026-10-22_14-05")
        ProcessStateFile.write(dir, ProcessState(id = "process-id"))

        val notice = ResultNotice.of(dir)

        assertEquals(dir.absolutePath, notice.tag)
        assertEquals(dir.absolutePath, notice.folder)
        assertNull(notice.id)
    }
}
