package dev.mnema.app.helper

import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.attribute.PosixFilePermission
import kotlin.coroutines.CoroutineContext
import kotlin.coroutines.EmptyCoroutineContext

@OptIn(ExperimentalCoroutinesApi::class)
class HelperClientTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val apk = 1_700_000_000_000L
    private val nonce = "0123456789abcdef0123456789abcdef"
    private var now = 500_000L

    private val control get() = folder.root
    private val statusFile get() = File(control, "status.json")
    private val commandFile get() = File(control, "command.txt")

    private class Gate : CoroutineDispatcher() {
        private val held = ArrayDeque<Runnable>()
        private var open = false

        override fun dispatch(context: CoroutineContext, block: Runnable) {
            if (open) block.run() else held.addLast(block)
        }

        fun runNext() = held.removeFirst().run()

        fun release() {
            open = true
            while (held.isNotEmpty()) held.removeFirst().run()
        }
    }

    private fun TestScope.client(dir: File = control) =
        HelperClient(dir, apk, { now }, backgroundScope, EmptyCoroutineContext)

    private fun TestScope.watch(client: HelperClient) {
        backgroundScope.launch { client.state.collect {} }
        runCurrent()
    }

    private fun statusJson(heartbeatUptimeMs: Long, apkUpdated: Long = apk, pong: String = "", nonce: String = this.nonce) =
        """{"v":1,"state":"idle","pid":4242,"nonce":"$nonce","apk_updated":$apkUpdated,""" +
            """"heartbeat_ms":1761046533000,"heartbeat_uptime_ms":$heartbeatUptimeMs,"pong":"$pong",""" +
            """"folder":"","last_folder":"2026-10-21_14-35","error":"","started_ms":0,"call_active":false,"mic_on":true,"call_on":true,"paused":false}"""

    private fun writeStatus(heartbeatUptimeMs: Long = now, apkUpdated: Long = apk, pong: String = "", nonce: String = this.nonce) {
        statusFile.writeText(statusJson(heartbeatUptimeMs, apkUpdated, pong, nonce))
    }

    private fun writeTracks(mic: Boolean, call: Boolean, state: String = "idle") {
        val json = statusJson(now).replace("\"state\":\"idle\"", "\"state\":\"$state\"")
            .replace("\"mic_on\":true,\"call_on\":true", "\"mic_on\":$mic,\"call_on\":$call")
        statusFile.writeText(json)
    }

    private fun TestScope.helperStep(ms: Long = 200) {
        advanceTimeBy(ms)
        runCurrent()
    }

    private val verb get() = commandFile.readText().substringBefore(' ')

    @Test
    fun mutationWaitsForDeadVerdictButMalformedStatusRemainsUnknown() = runTest {
        statusFile.writeText(statusJson(now - HelperProtocol.ALIVE_MS - 1)
            .replace("\"state\":\"idle\"", "\"state\":\"recording\"")
            .replace("\"folder\":\"\"", "\"folder\":\"record\""))
        val client = client()
        assertThrows(IOException::class.java) { client.recordingFolderForMutation() }
        val checking = async { client.prepareMutation() }
        runCurrent()
        assertThrows(IOException::class.java) { client.recordingFolderForMutation() }
        advanceTimeBy(HelperProtocol.PING_WAIT_MS + 1)
        runCurrent()
        checking.await()
        assertNull(client.recordingFolderForMutation())
        val ping = commandFile.readText()
        client.prepareMutation()
        assertEquals(ping, commandFile.readText())
        statusFile.writeText("{broken")
        client.prepareMutation()
        assertThrows(IOException::class.java) { client.recordingFolderForMutation() }
    }

    @Test
    fun mutationKeepsFreshRecordingBusy() = runTest {
        statusFile.writeText(statusJson(now)
            .replace("\"state\":\"idle\"", "\"state\":\"recording\"")
            .replace("\"folder\":\"\"", "\"folder\":\"record\""))
        val client = client()
        client.prepareMutation()
        assertEquals("record", client.recordingFolderForMutation())
        assertFalse(commandFile.exists())
    }

    @Test
    fun missingStatusMeansNotRunning() = runTest {
        val client = client()
        watch(client)

        assertEquals(HelperState.NotRunning, client.state.value)
        assertNull(client.readStatus())
    }

    @Test
    fun freshHeartbeatMeansRunning() = runTest {
        writeStatus(heartbeatUptimeMs = now - 1000)
        val client = client()
        watch(client)

        val state = client.state.value as HelperState.Running
        assertFalse(state.outdated)
        assertEquals("2026-10-21_14-35", state.status.lastFolder)
        assertFalse(state.status.recording)
        assertFalse("a fresh heartbeat needs no ping", commandFile.exists())
    }

    @Test
    fun otherApkUpdateTimeMeansOutdated() = runTest {
        writeStatus(apkUpdated = apk - 1)
        val client = client()
        watch(client)

        assertTrue((client.state.value as HelperState.Running).outdated)
    }

    @Test
    fun truncatedJsonKeepsPreviousState() = runTest {
        writeStatus()
        val client = client()
        watch(client)
        val running = client.state.value
        assertTrue(running is HelperState.Running)

        statusFile.writeText(statusJson(now).take(60))
        advanceTimeBy(2000)
        runCurrent()

        assertEquals(running, client.state.value)
        assertNull(client.readStatus())
    }

    @Test
    fun staleHeartbeatWithoutPongIsNotRunningOnlyAfterPingWait() = runTest {
        writeStatus()
        val client = client()
        watch(client)
        val running = client.state.value
        assertTrue(running is HelperState.Running)

        now += HelperProtocol.ALIVE_MS
        advanceTimeBy(500)
        runCurrent()
        val ping = commandFile.readText()
        assertTrue(ping, Regex("^ping [0-9a-f-]{36}$").matches(ping))

        advanceTimeBy(HelperProtocol.PING_WAIT_MS - 100)
        runCurrent()
        assertEquals("while waiting for pong the state stays", running, client.state.value)

        advanceTimeBy(200)
        runCurrent()
        assertEquals(HelperState.NotRunning, client.state.value)

        advanceTimeBy(5000)
        runCurrent()
        assertEquals("the same stale heartbeat — no second ping", ping, commandFile.readText())
        assertEquals(HelperState.NotRunning, client.state.value)
    }

    @Test
    fun staleHeartbeatAnsweredWithPongIsRunning() = runTest {
        writeStatus(heartbeatUptimeMs = now - 10_000)
        val client = client()
        watch(client)
        assertEquals(HelperState.Unknown, client.state.value)

        val uuid = commandFile.readText().removePrefix("ping ")
        writeStatus(heartbeatUptimeMs = now - 10_000, pong = uuid)
        advanceTimeBy(300)
        runCurrent()

        val state = client.state.value as HelperState.Running
        assertEquals(uuid, state.status.pong)
        assertFalse(state.outdated)
    }

    private fun TestScope.record(client: HelperClient, seen: MutableList<HelperState>) =
        backgroundScope.launch { client.state.collect { seen += it } }

    private fun TestScope.pingsDuring(ms: Long): Set<String> {
        val pings = mutableSetOf<String>()
        repeat((ms / 100).toInt()) {
            advanceTimeBy(100)
            runCurrent()
            if (commandFile.exists()) pings += commandFile.readText()
        }
        return pings
    }

    @Test
    fun resubscribeKeepsNotRunningForSameStaleHeartbeat() = runTest {
        writeStatus(heartbeatUptimeMs = now - 10_000)
        val client = client()
        val first = backgroundScope.launch { client.state.collect {} }
        runCurrent()
        advanceTimeBy(HelperProtocol.PING_WAIT_MS + 100)
        runCurrent()
        assertEquals(HelperState.NotRunning, client.state.value)
        val ping = commandFile.readText()
        first.cancel()
        runCurrent()

        val seen = mutableListOf<HelperState>()
        record(client, seen)
        runCurrent()
        val pings = pingsDuring(HelperProtocol.PING_WAIT_MS * 3)

        assertEquals(listOf(HelperState.NotRunning), seen)
        assertTrue("at most one new ping: $pings", (pings - ping).size <= 1)
    }

    @Test
    fun unsubscribeDuringPingWaitPingsAgainOnResubscribe() = runTest {
        writeStatus(heartbeatUptimeMs = now - 10_000)
        val client = client()
        val first = backgroundScope.launch { client.state.collect {} }
        runCurrent()
        val ping = commandFile.readText()
        advanceTimeBy(HelperProtocol.PING_WAIT_MS / 2)
        runCurrent()
        first.cancel()
        runCurrent()

        watch(client)
        val pings = pingsDuring(HelperProtocol.PING_WAIT_MS + 500)

        assertEquals(HelperState.NotRunning, client.state.value)
        assertEquals(1, (pings - ping).size)
    }

    @Test
    fun resubscribeDoesNotFlashUnknownForLiveHelper() = runTest {
        writeStatus()
        val client = HelperClient(control, apk, { now }, backgroundScope, StandardTestDispatcher(testScheduler))
        val first = backgroundScope.launch { client.state.collect {} }
        advanceTimeBy(100)
        runCurrent()
        assertTrue(client.state.value is HelperState.Running)
        first.cancel()
        runCurrent()

        val seen = mutableListOf<HelperState>()
        record(client, seen)
        advanceTimeBy(2000)
        runCurrent()

        assertTrue(seen.toString(), seen.isNotEmpty() && seen.all { it is HelperState.Running })
    }

    @Test
    fun pingWritesCommandInsideIoContext() = runTest {
        val io = Gate()
        val client = HelperClient(control, apk, { now }, backgroundScope, io)

        val result = async { client.ping(timeoutMs = 1000) }
        runCurrent()
        val writtenBeforeHop = commandFile.exists()
        io.release()

        assertFalse("command written outside ioContext", writtenBeforeHop)
        assertTrue(commandFile.readText().startsWith("ping "))
        assertFalse(result.await())
    }

    @Test
    fun pingForStaleHeartbeatIsWrittenInsideIoContext() = runTest {
        writeStatus(heartbeatUptimeMs = now - 10_000)
        val io = Gate()
        val client = HelperClient(control, apk, { now }, backgroundScope, io)

        watch(client)
        io.runNext()
        runCurrent()
        val writtenBeforeHop = commandFile.exists()
        io.release()

        assertFalse("command written outside ioContext", writtenBeforeHop)
        assertTrue(commandFile.readText().startsWith("ping "))
    }

    @Test
    fun staleHeartbeatPingDoesNotOverwriteAnUnacknowledgedStartOrStop() = runTest {
        for (command in listOf(HelperCommand.START, HelperCommand.STOP)) {
            writeTracks(mic = true, call = true, state = if (command == HelperCommand.START) "idle" else "recording")
            statusFile.writeText(statusFile.readText().replace("\"heartbeat_uptime_ms\":$now", "\"heartbeat_uptime_ms\":${now - 10_000}"))
            val writer = client()
            val pending = async { writer.command(command) }
            runCurrent()
            val sent = commandFile.readText()
            assertEquals(command.wire, verb)

            val monitor = client()
            val watching = backgroundScope.launch { monitor.state.collect {} }
            runCurrent()
            assertEquals("the connection check waits for the recording command to be confirmed", sent, commandFile.readText())

            writeTracks(mic = true, call = true, state = if (command == HelperCommand.START) "recording" else "idle")
            helperStep()
            assertTrue(pending.await())
            assertEquals("ping", verb)
            val uuid = commandFile.readText().removePrefix("ping ")
            statusFile.writeText(statusFile.readText().replace("\"pong\":\"\"", "\"pong\":\"$uuid\""))
            helperStep()
            assertTrue(monitor.state.value is HelperState.Running)
            watching.cancel()
            runCurrent()
        }
    }

    @Test
    fun startWaitsForAnotherClientsPingAcknowledgement() = runTest {
        writeStatus()
        val monitor = client()
        val ping = async { monitor.ping() }
        runCurrent()
        val sent = commandFile.readText()
        assertEquals("ping", verb)

        val writer = client()
        val start = async { writer.command(HelperCommand.START) }
        runCurrent()
        assertEquals("a recording does not overwrite an unread connection check", sent, commandFile.readText())

        writeStatus(pong = sent.removePrefix("ping "))
        helperStep()
        assertTrue(ping.await())
        assertEquals("start", verb)
        writeTracks(mic = true, call = true, state = "recording")
        helperStep()
        assertTrue(start.await())
    }

    @Test
    fun foreignProtocolVersionMeansOutdatedRunningWithoutPing() = runTest {
        val foreign = statusJson(now).replaceFirst("\"v\":1", "\"v\":2")
        statusFile.writeText(foreign)
        val client = client()
        watch(client)

        val state = client.state.value as HelperState.Running
        assertTrue(state.outdated)
        assertNull("another protocol is not read as ours", client.readStatus())

        now += 60_000
        advanceTimeBy(2000)
        runCurrent()
        assertTrue((client.state.value as HelperState.Running).outdated)
        assertFalse("another protocol is not pinged", commandFile.exists())
    }

    @Test
    fun heartbeatFromBeforeRebootIsDoubted() = runTest {
        writeStatus(heartbeatUptimeMs = now + 1000)
        val client = client()
        watch(client)

        assertTrue(commandFile.readText().startsWith("ping "))
        advanceTimeBy(HelperProtocol.PING_WAIT_MS + 100)
        runCurrent()
        assertEquals(HelperState.NotRunning, client.state.value)
    }

    @Test
    fun sendWritesCommandReadableByAllButWritableByNoOne() = runTest {
        val client = client()

        val uuid = client.send(HelperCommand.START)

        val text = commandFile.readText()
        assertEquals("start $uuid", text)
        assertTrue(text, Commands.parse(text) != null)
        val permissions = Files.getPosixFilePermissions(commandFile.toPath())
        assertTrue(permissions.toString(), PosixFilePermission.OTHERS_READ in permissions)
        assertFalse(permissions.toString(), PosixFilePermission.OTHERS_WRITE in permissions)
        assertFalse(permissions.toString(), PosixFilePermission.GROUP_WRITE in permissions)
        assertEquals(listOf("command.txt"), control.list()!!.toList())

        val next = client.send(HelperCommand.STOP)
        assertEquals("stop $next", commandFile.readText())
        assertFalse(uuid == next)
    }

    @Test
    fun sendDoesNotWriteThroughSymlinkedTemporaryFile() = runTest {
        val dir = folder.newFolder("control")
        val victim = folder.newFile("victim.txt").apply { writeText("не трогать") }
        val before = Files.getPosixFilePermissions(victim.toPath())
        Files.createSymbolicLink(File(dir, "command.txt.tmp").toPath(), victim.toPath())

        val uuid = client(dir).send(HelperCommand.STOP)

        assertEquals("не трогать", victim.readText())
        assertEquals(before, Files.getPosixFilePermissions(victim.toPath()))
        val command = File(dir, "command.txt")
        assertFalse(Files.isSymbolicLink(command.toPath()))
        assertEquals("stop $uuid", command.readText())
    }

    @Test
    fun sendFailsLoudlyAndPingQuietlyWithoutControlDir() = runTest {
        val client = client(File(control, "missing"))

        assertThrows(IOException::class.java) { client.send(HelperCommand.PING) }
        assertFalse(client.ping())
        val failure = runCatching { client.command(HelperCommand.STOP) }.exceptionOrNull()
        assertTrue(failure.toString(), failure is IOException)
        assertFalse(client.applySources { SourceChoice(mic = false) })
    }

    @Test
    fun awaitNonceWaitsForOwnNonceOnly() = runTest {
        val fresh = "fedcba9876543210fedcba9876543210"
        val client = client()
        val result = async { client.awaitNonce(fresh) }
        runCurrent()
        assertFalse(result.isCompleted)

        writeStatus(nonce = nonce)
        advanceTimeBy(500)
        runCurrent()
        assertFalse("another nonce is the previous helper", result.isCompleted)

        writeStatus(nonce = fresh)
        advanceTimeBy(500)
        runCurrent()
        assertTrue(result.await())

        assertFalse("the helper did not answer", client.awaitNonce("00112233445566778899aabbccddeeff", timeoutMs = 1000))
    }

    @Test
    fun nextSourceCommandClosesOneDifferenceAtATime() {
        val both = HelperStatus(v = 1, micOn = true, callOn = true)

        assertEquals(HelperCommand.MIC_OFF, nextSourceCommand(both, SourceChoice(mic = false, call = false)))
        assertEquals(HelperCommand.CALL_OFF, nextSourceCommand(both.copy(micOn = false), SourceChoice(mic = false, call = false)))
        assertEquals(HelperCommand.MIC_ON, nextSourceCommand(both.copy(micOn = false), SourceChoice()))
        assertEquals(HelperCommand.CALL_ON, nextSourceCommand(both.copy(callOn = false), SourceChoice()))
        assertNull(nextSourceCommand(both, SourceChoice()))
    }

    @Test
    fun sourcesAreSentOneAtATimeEachAwaitedInTheHeartbeat() = runTest {
        writeTracks(mic = true, call = true)
        val client = client()

        val result = async { client.applySources { SourceChoice(mic = false, call = false) } }
        runCurrent()
        val first = commandFile.readText()
        assertEquals("mic-off", verb)
        helperStep(1000)
        assertEquals("until the helper applied the first, the second does not overwrite it", first, commandFile.readText())

        writeTracks(mic = false, call = true)
        helperStep()
        assertEquals("call-off", verb)
        assertTrue(Commands.parse(commandFile.readText()) != null)
        writeTracks(mic = false, call = false)
        helperStep()

        assertTrue(result.await())
    }

    @Test
    fun matchingChoiceSendsNothing() = runTest {
        writeTracks(mic = false, call = true)

        assertTrue(client().applySources { SourceChoice(mic = false, call = true) })
        assertFalse(commandFile.exists())
    }

    @Test
    fun choiceChangedWhileSendingIsFollowedToTheEnd() = runTest {
        writeTracks(mic = true, call = true)
        var choice = SourceChoice(mic = false)
        val client = client()

        val result = async { client.applySources { choice } }
        runCurrent()
        assertEquals("mic-off", verb)
        choice = SourceChoice(mic = false, call = false)
        writeTracks(mic = false, call = true)
        helperStep()
        assertEquals("call-off", verb)
        writeTracks(mic = false, call = false)
        helperStep()

        assertTrue(result.await())
    }

    @Test
    fun helperThatDoesNotApplyTheChoiceIsGivenUpOn() = runTest {
        writeTracks(mic = true, call = true)
        val client = client()

        val result = async { client.applySources { SourceChoice(mic = false, call = false) } }
        helperStep(HelperProtocol.PING_WAIT_MS + 100)

        assertFalse(result.await())
        assertEquals("after a refusal nothing more is sent", "mic-off", verb)
    }

    @Test
    fun noStatusMeansChoiceIsNotSent() = runTest {
        assertFalse(client().applySources { SourceChoice(mic = false) })
        assertFalse(commandFile.exists())
    }

    @Test
    fun startGoesOnlyAfterTheChoiceIsApplied() = runTest {
        writeTracks(mic = true, call = true)
        val client = client()

        val result = async { client.command(HelperCommand.START, SourceChoice(mic = true, call = false)) }
        runCurrent()
        assertEquals("call-off", verb)
        writeTracks(mic = true, call = false)
        helperStep()
        assertEquals("start", verb)
        writeTracks(mic = true, call = false, state = "recording")
        helperStep()

        assertTrue(result.await())
    }

    @Test
    fun startIsNotSentWhenTheHelperDoesNotApplyTheChoice() = runTest {
        writeTracks(mic = true, call = true)
        val client = client()

        val result = async { client.command(HelperCommand.START, SourceChoice(mic = false)) }
        helperStep(HelperProtocol.PING_WAIT_MS + 100)
        helperStep(HelperProtocol.PING_WAIT_MS + 100)

        assertFalse(result.await())
        assertEquals("the microphone is not off — the recording does not start", "mic-off", verb)
    }

    private fun writePaused(paused: Boolean) {
        val json = statusJson(now).replace("\"state\":\"idle\"", "\"state\":\"recording\"")
            .replace("\"paused\":false", "\"paused\":$paused,\"paused_ms\":0")
        statusFile.writeText(json)
    }

    @Test
    fun pauseAndResumeAreDoneWhenTheHeartbeatShowsThem() = runTest {
        writePaused(false)
        val client = client()

        val pause = async { client.command(HelperCommand.PAUSE) }
        runCurrent()
        assertEquals("pause", verb)
        helperStep(1000)
        assertFalse("the heartbeat is not paused yet", pause.isCompleted)
        writePaused(true)
        helperStep()
        assertTrue(pause.await())
        assertEquals(true, client.readStatus()?.paused)

        val resume = async { client.command(HelperCommand.RESUME) }
        runCurrent()
        assertEquals("resume", verb)
        assertTrue(Commands.parse(commandFile.readText()) != null)
        writePaused(false)
        helperStep()
        assertTrue(resume.await())
    }

    @Test
    fun commandsFromDifferentClientsDoNotOverwriteEachOther() = runTest {
        writeTracks(mic = true, call = true, state = "recording")
        val screen = client()
        val notification = client()

        val sources = async { screen.applySources { SourceChoice(mic = false) } }
        runCurrent()
        val stop = async { notification.command(HelperCommand.STOP) }
        helperStep(500)
        assertEquals("mic-off", verb)

        writeTracks(mic = false, call = true, state = "recording")
        helperStep()
        assertEquals("stop", verb)
        writeTracks(mic = false, call = true)
        helperStep()

        assertTrue(sources.await())
        assertTrue(stop.await())
    }

    @Test
    fun unnoticedCommandIsStillSent() = runTest {
        val result = async { client().command(HelperCommand.STOP) }
        helperStep(HelperProtocol.PING_WAIT_MS + 100)

        assertFalse(result.await())
        assertEquals("stop", verb)
    }
}
