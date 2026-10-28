package dev.mnema.app.processing

import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class AacPumpTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val samples = ShortArray(1_000_000) { (it % 2000 - 1000).toShort() }
    private val inputs = (samples.size * 2 + CAPACITY - 1) / CAPACITY

    @Test
    fun everyReadyOutputIsTakenWithoutWaiting() {
        val codec = FakeCodec()
        val sink = FakeSink()

        pumpAac(ArrayPcm(samples), codec, sink)

        assertEquals(1, sink.starts)
        assertEquals(List(inputs * FRAMES) { "frame-$it" }, sink.frames)
        assertTrue(codec.endQueued)
        assertTrue("waited ${codec.waitedUs} µs for $inputs inputs", codec.waitedUs <= inputs * 10_000L / 4)
    }

    @Test
    fun inputFillsTheCodecBufferAndKeepsTheSoundAndItsTime() {
        val codec = FakeCodec()

        pumpAac(ArrayPcm(samples), codec, FakeSink())

        assertEquals(inputs, codec.inputSizes.size)
        assertTrue(codec.inputSizes.dropLast(1).all { it == CAPACITY })
        assertArrayEquals(pcm(samples), codec.fed.toByteArray())
        assertEquals(List(inputs) { it * (CAPACITY / 2) * 1_000_000L / RATE }, codec.inputPts)
    }

    @Test
    fun busyCodecIsWaitedForNotPolledInALoop() {
        val codec = FakeCodec(encodeUs = 3_000)
        val sink = FakeSink()

        pumpAac(ArrayPcm(samples), codec, sink)

        assertEquals(List(inputs * FRAMES) { "frame-$it" }, sink.frames)
        assertTrue("idle ${codec.idleUs} µs", codec.idleUs <= inputs * 3_000L + 10_000)
        assertTrue("calls ${codec.calls}", codec.calls <= inputs * (FRAMES + 6) + 20)
    }

    @Test
    fun emptySoundStillEndsTheStream() {
        val codec = FakeCodec()
        val sink = FakeSink()

        pumpAac(ArrayPcm(ShortArray(0)), codec, sink)

        assertTrue(codec.endQueued)
        assertEquals(emptyList<String>(), sink.frames)
    }

    @Test
    fun interruptedEncodingLeavesNeitherTheFileNorItsTemporary() {
        val out = File(tmp.root, "id-mic-1-2.m4a")

        val stopped = runCatching {
            encodeInto(out) { partial ->
                partial.writeBytes(ByteArray(1000))
                throw InterruptedException()
            }
        }.exceptionOrNull()

        assertTrue(stopped is InterruptedException)
        assertFalse(out.exists())
        assertEquals(emptyList<String>(), tmp.root.list()!!.toList())
    }

    @Test
    fun finishedEncodingReplacesTheFileInOneMove() {
        val out = File(tmp.root, "id-mic-1-2.m4a").apply { writeText("old") }

        encodeInto(out) { it.writeText("new") }

        assertEquals("new", out.readText())
        assertEquals(listOf(out.name), tmp.root.list()!!.toList())
    }

    private class ArrayPcm(private val samples: ShortArray) : PcmSource {
        override val sampleRate = RATE
        private var position = 0

        override fun read(buffer: ShortArray, max: Int): Int {
            val n = minOf(max, buffer.size, samples.size - position)
            samples.copyInto(buffer, 0, position, position + n)
            position += n
            return n
        }

        override fun close() = Unit
    }

    // a codec without Android: FRAMES frames in, an input is free once its frames are taken;
    // virtual time: only waiting calls advance it, the codec spends encodeUs on an input
    private class FakeCodec(private val encodeUs: Long = 0) : Codec {
        var waitedUs = 0L
        var idleUs = 0L
        var calls = 0
        var endQueued = false
        val fed = ByteArrayOutputStream()
        val inputSizes = mutableListOf<Int>()
        val inputPts = mutableListOf<Long>()

        private class Pending(
            val slot: Int,
            val data: ByteArray,
            val pts: Long,
            val config: Boolean,
            val end: Boolean,
            val readyAt: Long,
            val last: Boolean,
        )

        private val buffers = List(SLOTS) { ByteBuffer.allocate(CAPACITY) }
        private val free = ArrayDeque((0 until SLOTS).toList())
        private val pending = ArrayDeque<Pending>()
        private val handed = mutableMapOf<Int, Pending>()
        private var now = 0L
        private var busyUntil = 0L
        private var frame = 0
        private var nextIndex = 0
        private var formatSent = false
        private var configSent = false

        override fun dequeueInput(timeoutUs: Long): Int {
            count()
            check(!endQueued) { "input after the end of stream" }
            free.removeFirstOrNull()?.let { return it }
            if (timeoutUs > 0) {
                waitedUs += timeoutUs
                idleUs += timeoutUs
                now += timeoutUs
            }
            return Codec.NONE
        }

        override fun inputBuffer(index: Int): ByteBuffer = buffers[index]

        override fun queueInput(index: Int, bytes: Int, ptsUs: Long, end: Boolean) {
            val readyAt = maxOf(now, busyUntil) + if (end) 0 else encodeUs
            busyUntil = readyAt
            if (end) {
                endQueued = true
                pending += Pending(index, ByteArray(0), ptsUs, config = false, end = true, readyAt = readyAt, last = true)
                return
            }
            val data = ByteArray(bytes)
            buffers[index].duplicate().apply { position(0) }.get(data)
            fed.write(data)
            inputSizes += bytes
            inputPts += ptsUs
            if (!configSent) {
                configSent = true
                pending += Pending(-1, "config".toByteArray(), 0, config = true, end = false, readyAt = readyAt, last = false)
            }
            repeat(FRAMES) {
                pending += Pending(index, "frame-${frame++}".toByteArray(), ptsUs, config = false, end = false, readyAt, last = it == FRAMES - 1)
            }
        }

        override fun dequeueOutput(into: CodecOutput, timeoutUs: Long): Int {
            count()
            val head = pending.firstOrNull()
            if (head != null && head.readyAt <= now) return handOut(into)
            if (timeoutUs > 0) {
                waitedUs += timeoutUs
                if (head != null && head.readyAt - now <= timeoutUs) {
                    idleUs += head.readyAt - now
                    now = head.readyAt
                    return handOut(into)
                }
                idleUs += timeoutUs
                now += timeoutUs
            }
            return Codec.NONE
        }

        override fun outputBuffer(index: Int): ByteBuffer = ByteBuffer.wrap(handed.getValue(index).data)

        override fun releaseOutput(index: Int) {
            val output = handed.remove(index) ?: error("output $index not handed out")
            if (output.last && output.slot >= 0) free.addLast(output.slot)
        }

        private fun handOut(into: CodecOutput): Int {
            if (!formatSent) {
                formatSent = true
                return Codec.FORMAT_CHANGED
            }
            val output = pending.removeFirst()
            val index = nextIndex++
            handed[index] = output
            into.offset = 0
            into.size = output.data.size
            into.ptsUs = output.pts
            into.config = output.config
            into.end = output.end
            return index
        }

        private fun count() {
            if (++calls > 1_000_000) throw AssertionError("the codec is polled in circles")
        }
    }

    private class FakeSink : AacSink {
        var starts = 0
        val frames = mutableListOf<String>()

        override fun start() {
            starts++
        }

        override fun write(buffer: ByteBuffer, output: CodecOutput) {
            check(starts == 1) { "frame before the format" }
            val bytes = ByteArray(output.size)
            buffer.position(output.offset)
            buffer.get(bytes)
            frames += String(bytes)
        }
    }

    private companion object {
        const val RATE = 16_000
        const val SLOTS = 4
        const val CAPACITY = 65_536
        const val FRAMES = 8

        fun pcm(samples: ShortArray): ByteArray =
            ByteBuffer.allocate(samples.size * 2).order(ByteOrder.LITTLE_ENDIAN).apply { asShortBuffer().put(samples) }.array()
    }
}
