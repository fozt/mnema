package dev.mnema.app.transcript

import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** A WAV in the helper's layout: a 44-byte header, PCM16 mono, nothing after the data. */
internal object WavFixture {
    const val RATE = 16000

    fun header(dataBytes: Int, declared: Int = dataBytes, riff: Int = 36 + dataBytes, rate: Int = RATE): ByteArray =
        ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN).apply {
            put("RIFF".toByteArray()).putInt(riff).put("WAVEfmt ".toByteArray())
            putInt(16).putShort(1).putShort(1).putInt(rate).putInt(rate * 2).putShort(2).putShort(16)
            put("data".toByteArray()).putInt(declared)
        }.array()

    fun pcm(samples: ShortArray): ByteArray =
        ByteBuffer.allocate(samples.size * 2).order(ByteOrder.LITTLE_ENDIAN).apply {
            samples.forEach { putShort(it) }
        }.array()

    /** [loud] — spans (s, s) of a square wave with amplitude [amplitude]; the rest is silence. */
    fun samples(seconds: Double, loud: List<Pair<Double, Double>> = emptyList(), amplitude: Int = 1000): ShortArray {
        val out = ShortArray((seconds * RATE).toInt())
        for ((from, to) in loud) {
            for (i in (from * RATE).toInt() until minOf((to * RATE).toInt(), out.size)) {
                out[i] = (if (i % 2 == 0) amplitude else -amplitude).toShort()
            }
        }
        return out
    }

    fun write(file: File, samples: ShortArray, declared: Int? = null): File {
        val data = pcm(samples)
        file.writeBytes(header(data.size, declared ?: data.size) + data)
        return file
    }
}
