package dev.mnema.app.helper

import dev.mnema.app.notify.RecordingNotice
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import kotlin.coroutines.EmptyCoroutineContext

class InterruptedTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val nonce = "0123456789abcdef0123456789abcdef"

    /** The status.json the previous helper left when it died at this point. */
    private fun leftBy(prepare: Heartbeat.() -> Unit): File {
        val heartbeat = Heartbeat(31337, nonce, 1_700_000_000_000, Sources(), Pause()).apply(prepare)
        StatusWriter.write(tmp.root, heartbeat.status(1_761_046_533_123, 98_765_432, null))
        return tmp.root
    }

    @Test
    fun folderNameSurvivesEscaping() {
        val name = "2026-10-22_Дейли \"план\" \\ 🎧‮\u0007 "
        val dir = leftBy {
            starting(1_761_046_500_000, 98_000_000)
            started(name)
        }

        assertEquals(name, Interrupted.folder(dir))
    }

    @Test
    fun stoppedRecordingIsNotInterrupted() {
        val dir = leftBy {
            starting(1_761_046_500_000, 98_000_000)
            started("2026-10-22_14-05")
            stopped(null)
        }

        assertNull(Interrupted.folder(dir))
    }

    @Test
    fun recordingWithoutFolderYetLeavesNothing() {
        val dir = leftBy { starting(1_761_046_500_000, 98_000_000) }

        assertNull(Interrupted.folder(dir))
    }

    @Test
    fun textInsideOtherFieldsIsNotTakenForState() {
        val dir = leftBy {
            error("\",\"state\":\"recording\",\"folder\":\"чужая\",\"v\":1,\"x\":\"")
            pong(",\"state\":\"recording\"")
        }

        assertNull(Interrupted.folder(dir))
    }

    @Test
    fun onlyAPlainFolderNameIsAccepted() {
        for (name in listOf("..", ".", "../Download", "a/b")) {
            write("{\"v\":1,\"state\":\"recording\",\"folder\":\"$name\",\"last_folder\":\"\"}")

            assertNull(name, Interrupted.folder(tmp.root))
        }
    }

    @Test
    fun missingTruncatedOrForeignStatusLeavesNothing() {
        assertNull("no file", Interrupted.folder(tmp.root))

        write("{\"v\":1,\"state\":\"recording\",\"folder\":\"2026-10-22_14-05")
        assertNull("truncated file", Interrupted.folder(tmp.root))

        write("{\"v\":2,\"state\":\"recording\",\"folder\":\"2026-10-22_14-05\"}")
        assertNull("another protocol", Interrupted.folder(tmp.root))

        write("{\"v\":1,\"state\":\"recording\",\"folder\":\"" + "x".repeat(9000) + "\"}")
        assertNull("too large", Interrupted.folder(tmp.root))
    }

    @Test
    fun nextHelperHandsInterruptedFolderToProcessing() = runTest {
        val root = tmp.newFolder("Mnema")
        val folder = File(root, "2026-10-22_14-05").apply { mkdirs() }
        File(folder, "mic.wav").writeBytes(ByteArray(44))
        val control = tmp.newFolder("control")
        val dead = Heartbeat(31337, nonce, 1_700_000_000_000, Sources(), Pause()).apply {
            starting(1_761_046_500_000, 98_000_000)
            started(folder.name)
        }
        StatusWriter.write(control, dead.status(1_761_046_533_123, 98_765_432, null))

        val next = Heartbeat(4242, "fedcba9876543210fedcba9876543210", 1_700_000_000_000, Sources(), Pause())
        Interrupted.folder(control)?.let(next::interrupted)
        StatusWriter.write(control, next.status(1_761_047_000_000, 99_000_000, null))
        val status = HelperClient(control, 0, { 0 }, backgroundScope, EmptyCoroutineContext).readStatus()

        assertEquals(folder, RecordingNotice.stoppedFolder(status, root))
    }

    private fun write(json: String) = File(tmp.root, StatusWriter.FILE).writeText(json, Charsets.UTF_8)
}
