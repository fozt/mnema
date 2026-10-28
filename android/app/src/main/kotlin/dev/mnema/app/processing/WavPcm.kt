package dev.mnema.app.processing

import dev.mnema.app.i18n.tr
import dev.mnema.app.transcript.Wav
import java.io.Closeable
import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** A stream of PCM16 mono samples: the file is not read into memory whole. */
interface PcmSource : Closeable {
    val sampleRate: Int

    /** How many samples went into [buffer], at most [max]; 0 — the end. */
    fun read(buffer: ShortArray, max: Int): Int
}

class WavPcm private constructor(private val raf: RandomAccessFile, override val sampleRate: Int, frames: Long) : PcmSource {
    private var left = frames
    private var bytes = ByteArray(0)

    override fun read(buffer: ShortArray, max: Int): Int {
        val n = minOf(left, max.toLong(), buffer.size.toLong()).toInt()
        if (n <= 0) return 0
        if (bytes.size < n * 2) bytes = ByteArray(n * 2)
        raf.readFully(bytes, 0, n * 2)
        ByteBuffer.wrap(bytes, 0, n * 2).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().get(buffer, 0, n)
        left -= n
        return n
    }

    override fun close() = raf.close()

    companion object {
        /** Only PCM16 mono — what the helper writes; anything else is IOException. */
        fun open(file: File): WavPcm {
            val info = Wav.info(file) ?: throw IOException(tr("{name}: not a WAV file", "name" to file.name))
            if (info.bits != 16 || info.channels != 1) throw IOException(tr("{name}: not PCM16 mono", "name" to file.name))
            val raf = RandomAccessFile(file, "r")
            raf.seek(info.dataOffset)
            return WavPcm(raf, info.sampleRate, info.frames)
        }
    }
}
