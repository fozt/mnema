package dev.mnema.app.helper

import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder

class WavWriterTest {
    @get:Rule
    val folder = TemporaryFolder()

    @Test
    fun closedFileHasCanonicalHeaderAndNothingAfterData() {
        val file = File(folder.root, "mic.wav")
        val samples = ShortArray(1600) { (it - 800).toShort() }

        WavWriter(file, 16000).use { wav -> wav.write(samples, samples.size) }

        val bytes = file.readBytes()
        assertEquals(44 + 3200, bytes.size)
        assertHeader(bytes, dataBytes = 3200)
        val data = ByteBuffer.wrap(bytes, 44, 3200).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer()
        assertEquals(-800, data.get(0).toInt())
        assertEquals(799, data.get(1599).toInt())
    }

    @Test
    fun headerIsCorrectAfterOneSecondWithoutClose() {
        val file = File(folder.root, "system.wav")
        val wav = WavWriter(file, 16000)
        try {
            val second = ShortArray(16000) { 1000 }
            wav.write(second, second.size)

            val bytes = file.readBytes()
            assertEquals(44 + 32000, bytes.size)
            assertHeader(bytes, dataBytes = 32000)
        } finally {
            wav.close()
        }
    }

    private fun assertHeader(bytes: ByteArray, dataBytes: Int) {
        val header = ByteBuffer.wrap(bytes, 0, 44).order(ByteOrder.LITTLE_ENDIAN)
        assertEquals("RIFF", String(bytes, 0, 4, Charsets.US_ASCII))
        assertEquals(36 + dataBytes, header.getInt(4))
        assertEquals("WAVEfmt ", String(bytes, 8, 8, Charsets.US_ASCII))
        assertEquals(16, header.getInt(16))
        assertEquals(1, header.getShort(20).toInt())
        assertEquals(1, header.getShort(22).toInt())
        assertEquals(16000, header.getInt(24))
        assertEquals(32000, header.getInt(28))
        assertEquals(2, header.getShort(32).toInt())
        assertEquals(16, header.getShort(34).toInt())
        assertEquals("data", String(bytes, 36, 4, Charsets.US_ASCII))
        assertEquals(dataBytes, header.getInt(40))
        assertEquals(44 + dataBytes, bytes.size)
    }
}
