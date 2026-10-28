package dev.mnema.app.transcript

import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class WavTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private fun file(bytes: ByteArray): File = tmp.newFile().apply { writeBytes(bytes) }

    private fun u32(bytes: ByteArray, offset: Int): Long =
        ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).getInt(offset).toLong() and 0xFFFFFFFFL

    @Test
    fun interruptedRecordingGetsItsLengthBack() {
        val data = ByteArray(3200) { (it % 50).toByte() }
        val wav = file(WavFixture.header(dataBytes = 3200, declared = 0, riff = 0) + data)

        assertTrue(Wav.repair(wav))

        val bytes = wav.readBytes()
        assertEquals(3200L, u32(bytes, 40))
        assertEquals(36L + 3200, u32(bytes, 4))
        assertArrayEquals(data, bytes.copyOfRange(44, bytes.size))
        assertEquals(1600L, Wav.info(wav)?.frames)
    }

    @Test
    fun intactFileIsLeftAlone() {
        val bytes = WavFixture.header(3200) + ByteArray(3200)
        val wav = file(bytes)

        assertFalse(Wav.repair(wav))

        assertArrayEquals(bytes, wav.readBytes())
    }

    @Test
    fun foreignTagAfterTheDataIsNotTouched() {
        val tag = "LIST".toByteArray() + byteArrayOf(4, 0, 0, 0) + "INFO".toByteArray()
        val bytes = WavFixture.header(3200) + ByteArray(3200) + tag
        val wav = file(bytes)

        assertFalse(Wav.repair(wav))

        assertArrayEquals(bytes, wav.readBytes())
    }

    @Test
    fun repairKeepsTheRecordingTime() {
        val wav = file(WavFixture.header(3200, declared = 0) + ByteArray(3200))
        wav.setLastModified(1_700_000_000_000L)

        assertTrue(Wav.repair(wav))

        assertEquals(1_700_000_000_000L, wav.lastModified())
    }

    @Test
    fun halfWrittenFrameIsDropped() {
        val wav = file(WavFixture.header(3201, declared = 0) + ByteArray(3201))

        assertTrue(Wav.repair(wav))

        assertEquals(3200L, u32(wav.readBytes(), 40))
    }

    @Test
    fun notOurLayoutIsNotRepaired() {
        assertFalse(Wav.repair(file(ByteArray(20))))
        assertFalse(Wav.repair(file("RIFF".toByteArray() + ByteArray(60))))
        val notPcm = WavFixture.header(3200, declared = 0).also { it[20] = 3 }
        assertFalse(Wav.repair(file(notPcm + ByteArray(3200))))
        assertFalse(Wav.repair(File(tmp.root, "нет такого.wav")))
    }

    @Test
    fun infoReadsTheCanonicalHeader() {
        val wav = file(WavFixture.header(3200) + ByteArray(3200))
        val info = Wav.info(wav)

        assertNotNull(info)
        assertEquals(16000, info!!.sampleRate)
        assertEquals(1, info.channels)
        assertEquals(16, info.bits)
        assertEquals(44L, info.dataOffset)
        assertEquals(1600L, info.frames)
        assertEquals(0.1, Wav.seconds(wav), 1e-9)
    }

    @Test
    fun infoSkipsForeignChunksBeforeTheData() {
        val canonical = WavFixture.header(3200)
        val list = "LIST".toByteArray() + byteArrayOf(5, 0, 0, 0) + "abcde".toByteArray() + byteArrayOf(0)
        val bytes = canonical.copyOfRange(0, 36) + list + canonical.copyOfRange(36, 44) + ByteArray(3200)

        val info = Wav.info(file(bytes))

        assertEquals(36L + list.size + 8, info?.dataOffset)
        assertEquals(1600L, info?.frames)
    }

    @Test
    fun infoDoesNotTrustALengthLargerThanTheFile() {
        val info = Wav.info(file(WavFixture.header(dataBytes = 3200, declared = -1) + ByteArray(1000)))

        assertEquals(500L, info?.frames)
    }

    @Test
    fun sliceCopiesTheSamplesOfTheRangeInTheSameFormat() {
        val samples = ShortArray(64_000) { (it % 700 - 350).toShort() }
        val src = WavFixture.write(File(tmp.root, "mic.wav"), samples)
        src.setLastModified(1_700_000_000_000L)
        val dst = File(tmp.root, "часть/mic.wav")

        Wav.slice(src, dst, 1.0, 2.5)

        val expected = WavFixture.header(48_000) + WavFixture.pcm(samples.copyOfRange(16_000, 40_000))
        assertArrayEquals(expected, dst.readBytes())
        assertEquals(1_700_000_000_000L, dst.lastModified())
    }

    @Test
    fun sliceStopsAtTheEndOfTheRecording() {
        val src = WavFixture.write(File(tmp.root, "mic.wav"), ShortArray(16_000) { 7 })

        Wav.slice(src, File(tmp.root, "tail.wav"), 0.5, 9.0)
        Wav.slice(src, File(tmp.root, "rest.wav"), 0.25, null)
        Wav.slice(src, File(tmp.root, "none.wav"), 3.0, 4.0)

        assertEquals(8_000L, Wav.info(File(tmp.root, "tail.wav"))?.frames)
        assertEquals(12_000L, Wav.info(File(tmp.root, "rest.wav"))?.frames)
        assertEquals(0L, Wav.info(File(tmp.root, "none.wav"))?.frames)
    }

    @Test
    fun infoOfAnythingButWavIsNull() {
        assertNull(Wav.info(file(ByteArray(0))))
        assertNull(Wav.info(file("not a wav at all, just text".toByteArray())))
        assertNull(Wav.info(File(tmp.root, "нет такого.wav")))
        assertNull(Wav.info(file(WavFixture.header(3200).copyOfRange(0, 30))))
    }
}
