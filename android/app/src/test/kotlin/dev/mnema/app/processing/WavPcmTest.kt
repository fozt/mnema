package dev.mnema.app.processing

import dev.mnema.app.transcript.WavFixture
import java.io.File
import org.junit.Assert.assertArrayEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import kotlin.random.Random

class WavPcmTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private fun wav(name: String, vararg samples: Short): File =
        WavFixture.write(File(tmp.root, name), shortArrayOf(*samples))

    private fun PcmSource.readAll(chunk: Int = 3): ShortArray {
        val out = mutableListOf<Short>()
        val buffer = ShortArray(chunk)
        while (true) {
            val n = read(buffer, chunk)
            if (n <= 0) return out.toShortArray()
            out += buffer.take(n)
        }
    }

    @Test
    fun blockReadsGiveTheSamplesOfASampleBySampleDecoding() {
        val random = Random(7)
        val samples = ShortArray(10_001) { random.nextInt(-32768, 32768).toShort() }
        samples[0] = Short.MIN_VALUE
        samples[1] = Short.MAX_VALUE
        val file = WavFixture.write(File(tmp.root, "odd.wav"), samples)
        val data = file.readBytes().copyOfRange(44, file.length().toInt())
        val expected = ShortArray(data.size / 2) { ((data[2 * it].toInt() and 0xFF) or (data[2 * it + 1].toInt() shl 8)).toShort() }

        for (chunk in listOf(4096, 7, 10_001, 20_000)) {
            assertArrayEquals("chunk $chunk", expected, WavPcm.open(file).use { it.readAll(chunk) })
        }
    }

    @Test
    fun dataWithAnOddByteEndsOnTheLastWholeSample() {
        val samples = shortArrayOf(5, -6, 7)
        val file = File(tmp.root, "half.wav").apply { writeBytes(WavFixture.header(7) + WavFixture.pcm(samples) + byteArrayOf(0x11)) }

        assertArrayEquals(samples, WavPcm.open(file).use { it.readAll(2) })
    }
}
