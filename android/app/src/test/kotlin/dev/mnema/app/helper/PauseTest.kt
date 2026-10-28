package dev.mnema.app.helper

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class PauseTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val heard: Short = 1000
    private val duringPause: Short = 7777

    private fun frame(size: Int, value: Short) = ShortArray(size) { value }

    private fun samples(wav: ByteArray): List<Short> =
        wav.copyOfRange(44, wav.size).toList().chunked(2) { (lo, hi) -> ((hi.toInt() shl 8) or (lo.toInt() and 0xff)).toShort() }

    @Test
    fun pausedFramesAreDroppedFromBothTracksAndTheyStayEqual() {
        val pause = Pause()
        val call = File(folder.root, "system.wav")
        val mic = File(folder.root, "mic.wav")
        WavWriter(call, 16000).use { callWav ->
            WavWriter(mic, 16000).use { micWav ->
                // track reads come in chunks of different length, like with two AudioRecords
                fun step(callCount: Int, micCount: Int, value: Short) {
                    val paused = pause.paused()
                    Pause.write(callWav, paused, true, frame(4800, value), callCount)
                    Pause.write(micWav, paused, true, frame(4800, value), micCount)
                }
                step(4800, 3200, heard)
                step(0, 1600, heard)
                assertTrue(pause.set(true, 1_000))
                step(4800, 4800, duringPause)
                step(2000, 0, duringPause)
                assertTrue(pause.set(false, 4_000))
                step(3000, 3000, heard)
            }
        }

        val callData = call.readBytes()
        val micData = mic.readBytes()
        assertEquals("only the sound before and after the pause got into the file", 44 + (4800 + 3000) * 2, callData.size)
        assertEquals("tracks of equal length", callData.size, micData.size)
        for (data in listOf(callData, micData)) assertTrue(samples(data).all { it == heard })
    }

    @Test
    fun switchedOffTrackIsStillSilenceOutsidePause() {
        val file = File(folder.root, "mic.wav")
        WavWriter(file, 16000).use { assertEquals(3, Pause.write(it, false, false, frame(3, heard), 3)) }

        assertTrue(file.readBytes().copyOfRange(44, 50).all { it == 0.toByte() })
    }

    @Test
    fun pausedTimeAddsUpUntilResume() {
        val pause = Pause()
        assertFalse(pause.paused())
        assertEquals(0L, pause.pausedMs(5_000))

        assertTrue(pause.set(true, 10_000))
        assertTrue(pause.paused())
        assertEquals("a pause in progress counts up to the heartbeat", 1_500L, pause.pausedMs(11_500))
        assertFalse("a repeated pause changes nothing", pause.set(true, 12_000))
        assertTrue(pause.set(false, 13_000))
        assertEquals(3_000L, pause.pausedMs(60_000))
        assertTrue(pause.set(true, 70_000))
        assertTrue(pause.set(false, 71_000))
        assertEquals(4_000L, pause.pausedMs(80_000))
        assertFalse(pause.set(false, 90_000))

        pause.reset()
        assertFalse(pause.paused())
        assertEquals(0L, pause.pausedMs(100_000))
    }
}
