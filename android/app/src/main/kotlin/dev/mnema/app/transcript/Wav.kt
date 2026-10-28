package dev.mnema.app.transcript

import dev.mnema.app.i18n.tr
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder

data class WavInfo(val sampleRate: Int, val channels: Int, val bits: Int, val dataOffset: Long, val frames: Long)

object Wav {
    /** The WAV header Mnema writes; a file without audio is no longer than this. */
    const val HEADER = 44L
    // PCM16 mono 16 kHz: the length of a file without a WAV header
    private const val BYTES_PER_SECOND = 32_000.0
    private const val RIFF_LIMIT = 0xFFFFFFFFL
    private const val FORMAT_PCM = 1
    private const val FORMAT_EXTENSIBLE = 0xFFFE
    private const val SLICE_CHUNK = 1 shl 16
    private val CHUNK_ID = Regex("[A-Za-z0-9 _]{4}")

    /** Parses chunks; the data length never exceeds the rest of the file. null: not a PCM WAV. */
    fun info(file: File): WavInfo? = try {
        RandomAccessFile(file, "r").use { parse(it, toEnd = false) }
    } catch (_: IOException) {
        null
    }

    /**
     * Desktop wav_seconds (fixture recording-start-v1, lengths): rate, channels and data size from the header; a data length
     * never written (the recording broke off) — to the end of the file; not a PCM WAV or unreadable — (size − 44) / 32000.
     */
    fun seconds(file: File): Double {
        val info = try { RandomAccessFile(file, "r").use { parse(it, toEnd = true) } } catch (_: IOException) { null }
        return info?.let { it.frames.toDouble() / it.sampleRate } ?: (maxOf(file.length() - HEADER, 0) / BYTES_PER_SECOND)
    }

    // toEnd: the header data length was never written, so the audio runs to the end of the file
    private fun parse(raf: RandomAccessFile, toEnd: Boolean): WavInfo? {
        val length = raf.length()
        val head = ByteArray(12)
        if (length < head.size) return null
        raf.readFully(head)
        if (!head.hasTag(0, "RIFF") || !head.hasTag(8, "WAVE")) return null
        var format: ByteBuffer? = null
        var pos = head.size.toLong()
        val chunk = ByteArray(8)
        while (pos + chunk.size <= length) {
            raf.seek(pos)
            raf.readFully(chunk)
            val size = chunk.littleEndian().getInt(4).toLong() and RIFF_LIMIT
            val body = pos + chunk.size
            when {
                chunk.hasTag(0, "fmt ") -> {
                    if (size < 16) return null
                    val bytes = ByteArray(16)
                    raf.seek(body)
                    raf.readFully(bytes)
                    format = bytes.littleEndian()
                }
                chunk.hasTag(0, "data") -> {
                    val available = length - body
                    val end = body + size + (size and 1)
                    val unfinished = size > available || (end < length && !chunkFollows(raf, end, length))
                    return format?.let { dataInfo(it, body, if (toEnd && unfinished) available else minOf(size, available)) }
                }
            }
            pos = body + size + (size and 1)
        }
        return null
    }

    private fun dataInfo(format: ByteBuffer, offset: Long, bytes: Long): WavInfo? {
        val tag = format.getShort(0).toInt() and 0xFFFF
        val channels = format.getShort(2).toInt() and 0xFFFF
        val rate = format.getInt(4)
        val bits = format.getShort(14).toInt() and 0xFFFF
        val frameBytes = channels * (bits / 8)
        if (tag != FORMAT_PCM && tag != FORMAT_EXTENSIBLE) return null
        if (rate <= 0 || bits % 8 != 0 || frameBytes <= 0) return null
        return WavInfo(rate, channels, bits, offset, bytes / frameBytes)
    }

    /** A track slice [startS, endS) in the same format, like `_slice_wav`; null — to the end. The file time matches the source. */
    fun slice(src: File, dst: File, startS: Double, endS: Double?) {
        val info = info(src) ?: throw IOException(tr("{name}: not a PCM WAV", "name" to src.name))
        val frameBytes = info.channels * (info.bits / 8)
        val first = minOf((startS * info.sampleRate).toLong(), info.frames)
        val last = if (endS == null) info.frames else minOf((endS * info.sampleRate).toLong(), info.frames)
        var left = maxOf(0L, last - first) * frameBytes
        dst.absoluteFile.parentFile?.mkdirs()
        RandomAccessFile(src, "r").use { input ->
            FileOutputStream(dst).use { out ->
                out.write(header(info, left))
                input.seek(info.dataOffset + first * frameBytes)
                val buffer = ByteArray(SLICE_CHUNK)
                while (left > 0) {
                    val n = input.read(buffer, 0, minOf(left, buffer.size.toLong()).toInt())
                    if (n <= 0) throw IOException(tr("{name}: the track is shorter than its header", "name" to src.name))
                    out.write(buffer, 0, n)
                    left -= n
                }
                out.fd.sync()
            }
        }
        dst.setLastModified(src.lastModified())
    }

    private fun header(info: WavInfo, dataBytes: Long): ByteArray {
        val frameBytes = info.channels * (info.bits / 8)
        return ByteBuffer.allocate(HEADER.toInt()).order(ByteOrder.LITTLE_ENDIAN).apply {
            put("RIFF".toByteArray(Charsets.ISO_8859_1)).putInt((36 + dataBytes).toInt())
            put("WAVEfmt ".toByteArray(Charsets.ISO_8859_1)).putInt(16)
            putShort(FORMAT_PCM.toShort()).putShort(info.channels.toShort()).putInt(info.sampleRate)
            putInt(info.sampleRate * frameBytes).putShort(frameBytes.toShort()).putShort(info.bits.toShort())
            put("data".toByteArray(Charsets.ISO_8859_1)).putInt(dataBytes.toInt())
        }.array()
    }

    /** Recomputes an unwritten length from the file size; leaves foreign layouts alone. true: fixed. */
    fun repair(file: File): Boolean = try {
        val data = brokenDataBytes(file)
        if (data == null) {
            false
        } else {
            val modified = file.lastModified()
            RandomAccessFile(file, "rw").use { raf ->
                raf.seek(4)
                raf.write(le32(36 + data))
                raf.seek(40)
                raf.write(le32(data))
                raf.fd.sync()
            }
            file.setLastModified(modified)
            true
        }
    } catch (_: IOException) {
        false
    }

    /** Audio bytes in a file of our layout with an unwritten length; null: nothing to fix or a foreign file. */
    private fun brokenDataBytes(file: File): Long? = RandomAccessFile(file, "r").use { raf ->
        val size = raf.length()
        val head = ByteArray(HEADER.toInt())
        if (size < HEADER) return null
        raf.readFully(head)
        val buf = head.littleEndian()
        val ours = head.hasTag(0, "RIFF") && head.hasTag(8, "WAVEfmt ") && buf.getInt(16) == 16 &&
            buf.getShort(20).toInt() == FORMAT_PCM && head.hasTag(36, "data") && size - 8 <= RIFF_LIMIT
        if (!ours) return null
        val align = maxOf(1, buf.getShort(32).toInt() and 0xFFFF)
        val declared = buf.getInt(40).toLong() and RIFF_LIMIT
        val data = (size - HEADER).let { it - it % align }
        if (declared >= data || chunkFollows(raf, HEADER + declared + declared % 2, size)) null else data
    }

    private fun le32(value: Long): ByteArray =
        ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putInt(value.toInt()).array()

    // a RIFF chunk starts at byte [at] (a tag, and audio after it means a foreign file), not audio past the header
    private fun chunkFollows(raf: RandomAccessFile, at: Long, size: Long): Boolean {
        if (at + 8 > size) return false
        val tail = ByteArray(8)
        raf.seek(at)
        raf.readFully(tail)
        if (!CHUNK_ID.matches(String(tail, 0, 4, Charsets.ISO_8859_1))) return false
        return (tail.littleEndian().getInt(4).toLong() and RIFF_LIMIT) <= size - (at + 8)
    }

    private fun ByteArray.littleEndian(): ByteBuffer = ByteBuffer.wrap(this).order(ByteOrder.LITTLE_ENDIAN)

    private fun ByteArray.hasTag(at: Int, tag: String): Boolean =
        tag.indices.all { i -> at + i < size && this[at + i] == tag[i].code.toByte() }
}
