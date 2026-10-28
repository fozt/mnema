package dev.mnema.app.helper

import java.io.IOException
import kotlin.math.abs
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CallTrackTest {
    /** A sound source behind the interface: each step returns the next size from [frames] as samples [value], or the error [failure]. */
    private class Sound(var value: Short = 0) : CallTrack.Input {
        var failure: Int? = null
        var frames = listOf(FRAME)
        var closed = false
        private var reads = 0

        override fun read(buffer: ShortArray): Int {
            failure?.let { return it }
            val count = frames[reads++ % frames.size]
            buffer.fill(value, 0, count)
            return count
        }

        override fun close() { closed = true }
    }

    private val policy = Sound()
    private val downlink = Sound(value = 700)
    private var opened = 0
    private var refusal: Exception? = null
    private val track = CallTrack(policy) {
        opened++
        refusal?.let { throw it }
        downlink.closed = false
        downlink
    }
    private var now = 1_000_000L
    private val buffer = ShortArray(4800)

    /** A recording loop step 100 ms after the previous one: what would go into system.wav. */
    private fun step(kind: String): List<Short> = buffer.take(advance(kind, 100))

    private fun steps(kind: String, ms: Long): List<Short> = (0 until ms / 100).flatMap { step(kind) }

    /** Steps over [ms]: how many samples would go into system.wav. */
    private fun advance(kind: String, ms: Long): Int {
        var written = 0
        repeat((ms / 100).toInt()) {
            now += 100
            written += track.read(buffer, kind, now)
        }
        return written
    }

    @Test
    fun cellularCallWithPolicyZerosIsRecordedFromDownlinkUntilTheCallEnds() {
        assertTrue("3 s of zeros are written as is", steps(CallMode.CELLULAR, CallTrack.ZEROS_MS).all { it == 0.toShort() })
        assertEquals(CallTrack.POLICY, track.source())

        val call = steps(CallMode.CELLULAR, 1_000)
        assertEquals(1, opened)
        assertEquals(CallTrack.DOWNLINK, track.source())
        assertTrue("the other side's voice in the same track", call.takeLast(FRAME).all { it == downlink.value })
        track.tick()
        repeat(9) {
            steps(CallMode.CELLULAR, 1_000)
            track.tick()
        }

        assertTrue("the call ended: AudioPolicy again", step(CallMode.NONE).all { it == 0.toShort() })
        assertEquals(CallTrack.POLICY, track.source())
        assertTrue(downlink.closed)
        val attempt = track.attempts().single()
        assertEquals("sound", attempt.result())
        assertEquals(10L, attempt.soundSeconds())

        steps(CallMode.CELLULAR, CallTrack.ZEROS_MS + 500)
        assertEquals("a new call — a new attempt", 2, opened)
    }

    @Test
    fun policyWithSoundKeepsRecordingTheCallAsBefore() {
        policy.value = 300

        val call = steps(CallMode.CELLULAR, 60_000)

        assertEquals(0, opened)
        assertEquals(CallTrack.POLICY, track.source())
        assertTrue(call.all { it == policy.value })
        assertTrue(track.attempts().isEmpty())
    }

    @Test
    fun messengerCallOrNoCallNeverTriesDownlink() {
        steps(CallMode.MESSENGER, 10_000)
        steps(CallMode.NONE, 10_000)
        steps(CallMode.RINGING, 10_000)

        assertEquals(0, opened)
    }

    @Test
    fun downlinkRefusalKeepsRecordingFromPolicyAndIsTriedAgainInTheSameCall() {
        refusal = IOException("VOICE_DOWNLINK AudioRecord not initialized")

        val call = steps(CallMode.CELLULAR, CallTrack.ZEROS_MS + 500)
        assertEquals(1, opened)
        assertEquals((CallTrack.ZEROS_MS + 500).toInt() / 100 * FRAME, call.size)
        assertEquals(CallTrack.POLICY, track.source())
        assertEquals("error", track.attempts().single().result())
        assertTrue(track.attempts().single().error()!!.contains("not initialized"))

        advance(CallMode.CELLULAR, CallTrack.RETRY_MS / 2)
        assertEquals("no attempts until the new attempt", 1, opened)
        refusal = null
        advance(CallMode.CELLULAR, CallTrack.RETRY_MS)

        assertEquals(2, opened)
        assertEquals("the call connected: downlink opened and records", CallTrack.DOWNLINK, track.source())
        assertEquals(listOf("error", "sound"), track.attempts().map { it.result() })
    }

    @Test
    fun downlinkThatRefusesAllTheTimeStopsBeingOpenedInTheRecording() {
        refusal = IOException("VOICE_DOWNLINK AudioRecord not initialized")

        advance(CallMode.CELLULAR, 60 * 60_000L)
        val tried = opened
        advance(CallMode.CELLULAR, 10 * 60_000L)
        step(CallMode.NONE)
        advance(CallMode.CELLULAR, 60_000)

        assertTrue(tried > 1)
        assertEquals("neither in this call nor in the next", tried, opened)
        assertEquals(tried, track.attempts().size)
    }

    @Test
    fun downlinkThatStartsWithZerosTakesOverOnceTheOtherSideIsHeard() {
        downlink.value = 0

        val connecting = steps(CallMode.CELLULAR, CallTrack.ZEROS_MS + CallTrack.PROBE_MS + 5_000)
        assertTrue(connecting.all { it == 0.toShort() })
        assertEquals(CallTrack.POLICY, track.source())

        downlink.value = 700
        assertTrue("the other side started speaking", step(CallMode.CELLULAR).all { it == downlink.value })

        assertEquals(1, opened)
        assertEquals(CallTrack.DOWNLINK, track.source())
        assertEquals("sound", track.attempts().single().result())
    }

    @Test
    fun downlinkWithOnlyZerosIsReportedWhenTheCallEnds() {
        downlink.value = 0

        val call = steps(CallMode.CELLULAR, CallTrack.ZEROS_MS + CallTrack.PROBE_MS + 1_000)
        step(CallMode.NONE)

        assertEquals(1, opened)
        assertTrue(downlink.closed)
        assertEquals(CallTrack.POLICY, track.source())
        assertEquals("zeros", track.attempts().single().result())
        assertTrue(call.all { it == 0.toShort() })
    }

    @Test
    fun downlinkReadErrorFallsBackToPolicyWithoutStoppingTheRecording() {
        steps(CallMode.CELLULAR, CallTrack.ZEROS_MS + 500)
        assertEquals(CallTrack.DOWNLINK, track.source())

        downlink.failure = -3
        val after = step(CallMode.CELLULAR)

        assertEquals(FRAME, after.size)
        assertEquals(CallTrack.POLICY, track.source())
        assertTrue(downlink.closed)
        assertEquals("error", track.attempts().single().result())

        downlink.failure = null
        advance(CallMode.CELLULAR, CallTrack.RETRY_MS + 500)
        assertEquals("a new attempt in the same call", CallTrack.DOWNLINK, track.source())
    }

    @Test
    fun downlinkThatStopsGivingSamplesKeepsTheTrackInTimeAndGivesItBackToPolicy() {
        steps(CallMode.CELLULAR, CallTrack.ZEROS_MS + 500)
        assertEquals(CallTrack.DOWNLINK, track.source())

        downlink.frames = listOf(0)
        val stalled = advance(CallMode.CELLULAR, CallTrack.STALL_MS - 500)
        assertEquals(CallTrack.DOWNLINK, track.source())
        assertTrue("the file grows level with AudioPolicy", abs(stalled - policyFor(CallTrack.STALL_MS - 500)) <= CallTrack.SLACK)

        val after = advance(CallMode.CELLULAR, 2_000)
        assertEquals(CallTrack.POLICY, track.source())
        assertEquals("error", track.attempts().single().result())
        assertEquals("no drift left", policyFor(CallTrack.STALL_MS - 500 + 2_000), stalled + after)
    }

    @Test
    fun downlinkOutOfStepWithPolicyIsWrittenAtThePaceOfPolicy() {
        var written = advance(CallMode.CELLULAR, CallTrack.ZEROS_MS + 500)
        var elapsed = CallTrack.ZEROS_MS + 500
        assertEquals(CallTrack.DOWNLINK, track.source())

        downlink.frames = listOf(0, 2 * FRAME)
        val jitter = steps(CallMode.CELLULAR, 60_000)
        written += jitter.size
        elapsed += 60_000
        assertTrue("chunks off beat — no inserted zeros", jitter.all { it == downlink.value })
        assertTrue(abs(written - policyFor(elapsed)) <= CallTrack.SLACK)

        for (frame in listOf(FRAME + FRAME / 4, FRAME - FRAME / 4)) {
            downlink.frames = listOf(frame)
            written += advance(CallMode.CELLULAR, 60_000)
            elapsed += 60_000
            assertTrue("downlink $frame per step", abs(written - policyFor(elapsed)) <= CallTrack.SLACK)
        }
        assertEquals(CallTrack.DOWNLINK, track.source())
    }

    @Test
    fun policyThatFindsItsSoundAgainTakesTheTrackBack() {
        steps(CallMode.CELLULAR, CallTrack.ZEROS_MS + 500)
        assertEquals(CallTrack.DOWNLINK, track.source())

        policy.value = 300
        assertTrue(step(CallMode.CELLULAR).all { it == policy.value })
        assertEquals(CallTrack.POLICY, track.source())
        assertTrue(downlink.closed)
    }

    @Test
    fun policyErrorIsReportedToTheRecording() {
        policy.failure = -6

        assertTrue(track.read(ShortArray(4800), CallMode.NONE, now) < 0)
        assertTrue(track.read(ShortArray(4800), CallMode.CELLULAR, now) < 0)
        assertEquals(0, opened)
    }

    private fun policyFor(ms: Long) = (ms / 100).toInt() * FRAME

    private companion object {
        const val FRAME = 1600
    }
}
