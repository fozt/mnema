package dev.mnema.app.ui

import dev.mnema.app.helper.HelperClient
import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.helper.SourceChoice
import dev.mnema.app.ui.record.pushSources
import dev.mnema.app.ui.record.startAfterSources
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.async
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import kotlin.coroutines.EmptyCoroutineContext

@OptIn(ExperimentalCoroutinesApi::class)
class SourceSyncTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val apk = 1_700_000_000_000L
    private val now = 500_000L
    private val statusFile get() = File(folder.root, "status.json")
    private val commandFile get() = File(folder.root, "command.txt")
    private val verb get() = commandFile.readText().substringBefore(' ')

    private fun TestScope.client() = HelperClient(folder.root, apk, { now }, backgroundScope, EmptyCoroutineContext)

    private fun writeTracks(mic: Boolean, call: Boolean, state: String = "idle") {
        statusFile.writeText(
            """{"v":1,"state":"$state","pid":4242,"nonce":"0123456789abcdef0123456789abcdef","apk_updated":$apk,""" +
                """"heartbeat_ms":1761046533000,"heartbeat_uptime_ms":$now,"pong":"","folder":"","last_folder":"",""" +
                """"error":"","started_ms":0,"call_active":false,"mic_on":$mic,"call_on":$call,"paused":false}""",
        )
    }

    private fun TestScope.commandsDuring(ms: Long): List<String> {
        val seen = mutableListOf<String>()
        repeat((ms / 100).toInt()) {
            advanceTimeBy(100)
            runCurrent()
            commandFile.takeIf { it.exists() }?.readText()?.let { if (seen.lastOrNull() != it) seen += it }
        }
        return seen
    }

    @Test
    fun helperThatNeverAppliesTheChoiceIsAskedThreeTimesThenGivenUpOn() = runTest {
        writeTracks(mic = true, call = true)
        val client = client()

        val result = async { pushSources(client) { SourceChoice(mic = false) } }
        val sent = commandsDuring(30_000)

        assertFalse(result.await())
        assertEquals(sent.toString(), 3, sent.size)
        assertTrue(sent.toString(), sent.all { it.startsWith("mic-off ") })
    }

    @Test
    fun helperThatAppliesTheChoiceOnTheSecondAttemptIsNotAskedAgain() = runTest {
        writeTracks(mic = true, call = true)
        val client = client()

        val result = async { pushSources(client) { SourceChoice(mic = false) } }
        val first = commandsDuring(HelperProtocol.PING_WAIT_MS + 2_500)
        writeTracks(mic = false, call = true)
        val later = commandsDuring(20_000)

        assertTrue(result.await())
        assertEquals((first + later).toString(), 2, (first + later).distinct().size)
    }

    @Test
    fun startIsNotSentWhileTheHelperIgnoresTheChoice() = runTest {
        writeTracks(mic = true, call = true)
        val client = client()

        val result = async { startAfterSources(client, SourceChoice(mic = false)) }
        commandsDuring(HelperProtocol.PING_WAIT_MS * 3)

        assertFalse(result.await())
        assertEquals("mic-off", verb)
    }

    @Test
    fun startGoesRightAfterTheChoiceIsApplied() = runTest {
        writeTracks(mic = true, call = true)
        val client = client()

        val result = async { startAfterSources(client, SourceChoice(mic = false)) }
        runCurrent()
        assertEquals("mic-off", verb)
        writeTracks(mic = false, call = true)
        commandsDuring(200)
        assertEquals("start", verb)
        writeTracks(mic = false, call = true, state = "recording")
        commandsDuring(200)

        assertTrue(result.await())
    }
}
