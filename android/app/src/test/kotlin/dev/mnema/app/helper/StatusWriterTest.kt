package dev.mnema.app.helper

import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.nio.file.Files
import java.nio.file.attribute.PosixFilePermission
import kotlin.coroutines.EmptyCoroutineContext

class StatusWriterTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val folderName = "2026-10-21_Дейли \"план\" \\ 🎧 שלום\u202e\u0007\n"
    private val errorText = "Не удалось начать запись: \"x\"\t\u2028"

    private fun recordingStatus() = StatusWriter.Status().apply {
        state = "recording"
        pid = 31337
        nonce = "0123456789abcdef0123456789abcdef"
        apkUpdated = 1_700_000_000_000
        heartbeatMs = 1_761_046_533_123
        heartbeatUptimeMs = 98_765_432
        pong = "123e4567-e89b-12d3-a456-426614174000"
        folder = folderName
        lastFolder = "2026-10-21_14-35 (2)"
        error = errorText
        startedMs = 1_761_046_500_000
        callActive = true
        callMode = CallMode.CELLULAR
        micOn = false
        callOn = true
        paused = true
        pausedMs = 4_000
        callRms = 1404.25
        micRms = 0.0
        micSilenced = false
        callSound = false
        micSound = true
        callSilentMs = 7_300
        callSource = CallTrack.DOWNLINK
    }

    @Test
    fun appReadsTheStatusTheHelperWrites() = runTest {
        val dir = folder.root
        val client = HelperClient(dir, 0, { 0 }, backgroundScope, EmptyCoroutineContext)
        for (silenced in listOf(false, null)) {
            StatusWriter.write(dir, recordingStatus().apply {
                micSilenced = silenced
                if (silenced == null) {
                    callSound = null
                    callSilentMs = null
                    callSource = null
                }
            })

            val read = client.readStatus()

            assertEquals(
                HelperStatus(
                    v = 1, state = "recording", nonce = "0123456789abcdef0123456789abcdef",
                    apkUpdated = 1_700_000_000_000, heartbeatMs = 1_761_046_533_123, heartbeatUptimeMs = 98_765_432,
                    pong = "123e4567-e89b-12d3-a456-426614174000", folder = folderName, lastFolder = "2026-10-21_14-35 (2)",
                    error = errorText, startedMs = 1_761_046_500_000, callActive = true, callMode = "cellular", micOn = false, callOn = true,
                    paused = true, pausedMs = 4_000, callRms = 1404.25, micRms = 0.0, micSilenced = silenced,
                    callSound = silenced, micSound = true, callSilentMs = if (silenced == null) null else 7_300,
                ),
                read,
            )
            assertTrue(read!!.recording)
            // pid and the call source are for adb cat status.json, the app does not read them
            val written = Json.parseToJsonElement(dir.resolve("status.json").readText()).jsonObject
            assertEquals(31337, written.getValue("pid").jsonPrimitive.int)
            assertEquals(if (silenced == null) null else "downlink", written.getValue("call_source").jsonPrimitive.contentOrNull)
        }
        val permissions = Files.getPosixFilePermissions(dir.resolve("status.json").toPath())
        assertTrue(PosixFilePermission.OTHERS_READ in permissions)
        assertFalse(PosixFilePermission.OTHERS_WRITE in permissions)
        assertEquals(listOf("status.json"), dir.list()!!.toList())
    }

    @Test
    fun missingRequiredSourceAndPauseFieldsAreInvalidStatus() = runTest {
        val client = HelperClient(folder.root, 0, { 0 }, backgroundScope, EmptyCoroutineContext)
        val complete = StatusWriter.json(recordingStatus())
        for (field in listOf("mic_on", "call_on", "paused")) {
            val missing = complete.replace(Regex("\"$field\":(?:true|false),"), "")
            folder.root.resolve("status.json").writeText(missing)
            assertNull(field, client.readStatus())
        }
    }

}
