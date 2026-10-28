package dev.mnema.app.processing

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.media.MediaMuxer
import dev.mnema.app.i18n.tr
import java.io.File
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** Compresses a track before sending it to the server; a failure is IOException, an interrupted thread InterruptedException. */
interface AudioEncoder {
    fun encode(wav: File, out: File)
}

/** A ready codec output: where the data is in the buffer, the frame time and flags; [flags] as MediaCodec gave them, for MediaMuxer. */
internal class CodecOutput {
    var offset = 0
    var size = 0
    var ptsUs = 0L
    var config = false
    var end = false
    var flags = 0
}

/** What the compression pump needs from MediaCodec. */
internal interface Codec {
    /** Index of a free input or [NONE]. */
    fun dequeueInput(timeoutUs: Long): Int

    fun inputBuffer(index: Int): ByteBuffer

    fun queueInput(index: Int, bytes: Int, ptsUs: Long, end: Boolean)

    /** Index of a ready output, [NONE] or [FORMAT_CHANGED]; [into] is filled for an index ≥ 0. */
    fun dequeueOutput(into: CodecOutput, timeoutUs: Long): Int

    fun outputBuffer(index: Int): ByteBuffer

    fun releaseOutput(index: Int)

    companion object {
        const val NONE = -1
        const val FORMAT_CHANGED = -2
    }
}

/** Where compressed frames go; [start] — the codec format is known. */
internal interface AacSink {
    fun start()

    fun write(buffer: ByteBuffer, output: CodecOutput)
}

private const val WAIT_US = 10_000L

/** All sound goes into the codec, every ready output is taken at once; wait only when the codec has nothing to give and nowhere to take. */
internal fun pumpAac(source: PcmSource, codec: Codec, sink: AacSink) = AacPump(source, codec, sink).run()

private class AacPump(private val source: PcmSource, private val codec: Codec, private val sink: AacSink) {
    private val output = CodecOutput()
    private var samples = ShortArray(0)
    private var queued = 0L
    private var inputDone = false
    private var started = false
    private var ended = false

    fun run() {
        while (!ended) {
            // cancelling the work interrupts the thread: encodeInto removes the temporary file
            if (Thread.interrupted()) throw InterruptedException()
            val fed = !inputDone && feed()
            if (drain(0) == 0 && !fed && !ended) drain(WAIT_US)
        }
    }

    private fun feed(): Boolean {
        val index = codec.dequeueInput(0)
        if (index < 0) return false
        val input = codec.inputBuffer(index)
        input.clear()
        val room = input.remaining() / 2
        if (room == 0) throw IOException(tr("The codec buffer is empty"))
        if (samples.size < room) samples = ShortArray(room)
        val n = source.read(samples, room)
        val pts = queued * 1_000_000L / source.sampleRate
        if (n <= 0) {
            codec.queueInput(index, 0, pts, end = true)
            inputDone = true
        } else {
            input.order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().put(samples, 0, n)
            codec.queueInput(index, n * 2, pts, end = false)
            queued += n
        }
        return true
    }

    /** How many outputs were taken; the first waits up to [timeoutUs], the rest without waiting. */
    private fun drain(timeoutUs: Long): Int {
        var taken = 0
        var wait = timeoutUs
        while (!ended) {
            val index = codec.dequeueOutput(output, wait)
            wait = 0
            when {
                index == Codec.FORMAT_CHANGED -> {
                    sink.start()
                    started = true
                }
                index >= 0 -> {
                    if (!output.config && output.size > 0 && started) sink.write(codec.outputBuffer(index), output)
                    codec.releaseOutput(index)
                    ended = output.end
                }
                else -> return taken
            }
            taken++
        }
        return taken
    }
}

/** Write to a temporary file next to it and swap: an interrupted compression does not look finished and does not stay in the cache. */
internal fun encodeInto(out: File, write: (File) -> Unit) {
    val temporary = File.createTempFile("${out.name}.", ".tmp", out.absoluteFile.parentFile)
    try {
        write(temporary)
        if (!temporary.renameTo(out)) throw IOException(tr("Couldn't save {name}", "name" to out.name))
    } finally {
        temporary.delete()
    }
}

/** AAC-LC, mono, 32 kbit/s in `.m4a`; the codec comes from `MediaCodecList.findEncoderForFormat`. */
class MediaCodecEncoder : AudioEncoder {
    override fun encode(wav: File, out: File) = WavPcm.open(wav).use { source -> encodeInto(out) { encode(source, it) } }

    private fun encode(source: PcmSource, out: File) {
        val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AAC, source.sampleRate, 1).apply {
            setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
            setInteger(MediaFormat.KEY_BIT_RATE, BIT_RATE)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, INPUT_BYTES)
        }
        val name = MediaCodecList(MediaCodecList.REGULAR_CODECS).findEncoderForFormat(format)
            ?: throw IOException(tr("No AAC encoder"))
        val codec = try {
            MediaCodec.createByCodecName(name)
        } catch (e: IllegalArgumentException) {
            throw IOException(tr("Encoder {name} is unavailable", "name" to name), e)
        }
        var muxer: MediaMuxer? = null
        try {
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            codec.start()
            muxer = MediaMuxer(out.path, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
            pumpAac(source, MediaCodecPipe(codec), MuxerSink(codec, muxer))
            muxer.stop()
        } catch (e: IllegalStateException) {
            throw IOException(tr("Compression failed"), e)
        } finally {
            runCatching { codec.stop() }
            codec.release()
            runCatching { muxer?.release() }
        }
    }

    private class MediaCodecPipe(private val codec: MediaCodec) : Codec {
        private val info = MediaCodec.BufferInfo()

        override fun dequeueInput(timeoutUs: Long): Int = codec.dequeueInputBuffer(timeoutUs).let { if (it >= 0) it else Codec.NONE }

        override fun inputBuffer(index: Int): ByteBuffer = checkNotNull(codec.getInputBuffer(index))

        override fun queueInput(index: Int, bytes: Int, ptsUs: Long, end: Boolean) =
            codec.queueInputBuffer(index, 0, bytes, ptsUs, if (end) MediaCodec.BUFFER_FLAG_END_OF_STREAM else 0)

        override fun dequeueOutput(into: CodecOutput, timeoutUs: Long): Int {
            val index = codec.dequeueOutputBuffer(info, timeoutUs)
            if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) return Codec.FORMAT_CHANGED
            if (index < 0) return Codec.NONE
            into.offset = info.offset
            into.size = info.size
            into.ptsUs = info.presentationTimeUs
            into.config = info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0
            into.end = info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0
            into.flags = info.flags
            return index
        }

        override fun outputBuffer(index: Int): ByteBuffer = checkNotNull(codec.getOutputBuffer(index))

        override fun releaseOutput(index: Int) = codec.releaseOutputBuffer(index, false)
    }

    private class MuxerSink(private val codec: MediaCodec, private val muxer: MediaMuxer) : AacSink {
        private val info = MediaCodec.BufferInfo()
        private var track = -1

        override fun start() {
            track = muxer.addTrack(codec.outputFormat)
            muxer.start()
        }

        override fun write(buffer: ByteBuffer, output: CodecOutput) {
            info.set(output.offset, output.size, output.ptsUs, output.flags)
            buffer.position(output.offset).limit(output.offset + output.size)
            muxer.writeSampleData(track, buffer, info)
        }
    }

    private companion object {
        const val BIT_RATE = 32_000
        const val INPUT_BYTES = 64 * 1024
    }
}
