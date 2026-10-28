package dev.mnema.app.helper

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class CommandsTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val uuid = "123e4567-e89b-12d3-a456-426614174000"

    private val rejected = listOf(
        "quit $uuid",
        "start",
        "start $uuid" + "0".repeat(158),
        "START $uuid",
        "start ../x",
        "start ${uuid.uppercase()}",
        "start $uuid\n",
        " start $uuid",
        "start  $uuid",
        "start $uuid; reboot",
        "mic $uuid",
        "mic-of $uuid",
        "mic_off $uuid",
        "MIC-OFF $uuid",
        "call-on",
        "speaker-off $uuid",
        "pause",
        "PAUSE $uuid",
        "paused $uuid",
        "resume  $uuid",
        "",
    )

    private val verbs = listOf("start", "stop", "ping", "mic-on", "mic-off", "call-on", "call-off", "pause", "resume")
    private val accepted = verbs.map { "$it $uuid" }

    @Test
    fun knownVerbWithUuidIsParsed() {
        for ((verb, raw) in verbs.zip(accepted)) {
            assertEquals(raw, verb, Commands.parse(raw))
            assertEquals(raw, uuid, Commands.uuid(raw))
        }
    }

    @Test
    fun anythingElseIsRejected() {
        assertEquals(200, rejected[2].toByteArray().size)
        for (raw in rejected) {
            assertNull(raw, Commands.parse(raw))
            assertNull(raw, Commands.uuid(raw))
        }
    }

    @Test
    fun readingStopsAfterTheSizeLimit() {
        val file = File(folder.root, "command.txt")
        assertEquals("", Commands.read(file))

        file.writeText("start $uuid")
        assertEquals("start $uuid", Commands.read(file))

        file.writeBytes(ByteArray(10_000) { 'a'.code.toByte() })
        val raw = Commands.read(file)
        assertTrue(raw.length <= 129)
        assertNull(Commands.parse(raw))
    }
}
