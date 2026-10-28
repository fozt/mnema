package dev.mnema.app.processing

import dev.mnema.app.recordings.ProcessingState
import dev.mnema.app.recordings.outcomeKey
import dev.mnema.app.recordings.recordingId
import dev.mnema.app.settings.settingsFor
import dev.mnema.app.transcript.Meta
import dev.mnema.app.server.RunnerClient
import dev.mnema.app.server.parseConnection
import dev.mnema.app.transcript.RecordingFolder
import dev.mnema.app.transcript.WavFixture
import java.io.File
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import mockwebserver3.SocketEffect
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class ResumableUploadTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val runner = MockWebServer()
    private val http = OkHttpClient()
    private val node = Node()
    private val progress = CopyOnWriteArrayList<ProcessingState>()

    @Volatile
    private var now = START_MS

    // how long compressing one track "takes" by the processing clock
    private var encodeMs = 0L

    @Before
    fun setUp() {
        runner.dispatcher = node
        runner.start()
    }

    @After
    fun tearDown() {
        runner.close()
        http.dispatcher.executorService.shutdown()
        http.connectionPool.evictAll()
    }

    private val cache: File get() = File(tmp.root, "cache")

    private fun settings() = settingsFor(runner.url("/").toString().trimEnd('/') + "#token=t")

    private fun pipeline(encoder: AudioEncoder, http: OkHttpClient) = Pipeline(
        settings = settings(),
        runner = RunnerClient(parseConnection(settings().connection), http),
        encoder = encoder,
        cacheDir = cache,
        clock = { now },
        recordingNow = { null },
    )

    private fun step(dir: File, deadline: Long = now + STEP_MS, encoder: AudioEncoder = CopyEncoder(), http: OkHttpClient = this.http) =
        runBlocking { pipeline(encoder, http).step(dir, deadline) { progress += it } }

    private fun recording(seconds: Double = 4.0, system: Boolean = false): File {
        val dir = File(File(tmp.root, "Mnema"), "2026-10-21_14-35").apply { mkdirs() }
        val loud = listOf(0.5 to seconds - 0.5)
        WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(seconds, loud = loud))
        if (system) WavFixture.write(File(dir, "system.wav"), WavFixture.samples(seconds, loud = loud))
        return dir
    }

    // every PUT "takes" [ms] by the processing clock
    private fun slowPuts(ms: Long): OkHttpClient = http.newBuilder().addInterceptor { chain ->
        if (chain.request().method == "PUT") now += ms
        chain.proceed(chain.request())
    }.build()

    @Test
    fun brokenConnectionResumesFromTheOffsetOfTheNode() {
        val dir = recording(seconds = LONG_S)
        node.dropAfter = 2

        val (first, _) = step(dir)

        assertEquals(StepResult.Failed(Problems.nodeDown(settings().activeEntry()!!.name)), first)
        val saved = ProcessStateFile.read(dir)!!
        assertEquals(ProcessStage.FAILED, saved.stage)
        assertEquals("u1", saved.tracks.getValue("mic").uploadId)
        // the answer to the second chunk was lost: the phone knows only about the first
        assertEquals(CHUNK, saved.tracks.getValue("mic").uploaded)
        assertEquals(listOf(0L, CHUNK), node.puts)

        node.down = false
        ProcessStateFile.write(dir, queued(ProcessStateFile.read(dir), false, id = recordingId(dir)!!, since = outcomeKey(Meta.read(dir))))
        node.attempts.clear()
        val (second, folder) = step(dir)

        assertEquals(StepResult.Done, second)
        assertEquals(listOf(2 * CHUNK), node.attempts)
        assertArrayEquals(File(folder, "mic.wav").readBytes(), node.uploads.getValue("u1").data)
        assertEquals(listOf("uploads/u1/mic.m4a"), node.submitted)
        assertTrue(File(folder, RecordingFolder.TRANSCRIPT).exists())
    }

    @Test
    fun endOfTheStepKeepsTheOffsetForTheNextStep() {
        val dir = recording(seconds = LONG_S)
        val slow = slowPuts(3 * MINUTE_MS)

        val (first, _) = step(dir, http = slow)

        assertEquals(StepResult.Again(1_000), first)
        val track = ProcessStateFile.read(dir)!!.tracks.getValue("mic")
        assertEquals("u1", track.uploadId)
        assertEquals(2 * CHUNK, track.uploaded)
        assertEquals(listOf(0L, CHUNK), node.puts)

        val (second, _) = step(dir, http = slow)

        assertEquals(StepResult.Done, second)
        assertEquals(listOf(0L, CHUNK, 2 * CHUNK), node.puts)
        assertEquals(1, node.calls.count { it == "POST /api/uploads" })
        assertEquals(listOf("uploads/u1/mic.m4a"), node.submitted)
    }

    @Test
    fun conflictContinuesFromTheOffsetTheNodeSends() {
        val dir = recording(seconds = LONG_S)
        node.conflictAt = 2 * CHUNK

        val (outcome, folder) = step(dir)

        assertEquals(StepResult.Done, outcome)
        assertEquals(listOf(0L, CHUNK, 2 * CHUNK, CHUNK, 2 * CHUNK), node.attempts)
        assertArrayEquals(File(folder, "mic.wav").readBytes(), node.uploads.getValue("u1").data)
    }

    @Test
    fun lostReplyToTheLastChunkTakesThePathFromTheNode() {
        val dir = recording(seconds = LONG_S)
        node.conflictOnLast = true

        val (outcome, folder) = step(dir)

        assertEquals(StepResult.Done, outcome)
        assertEquals(1, node.calls.count { it == "POST /api/uploads" })
        assertEquals(listOf(0L, CHUNK, 2 * CHUNK), node.puts)
        assertTrue(node.calls.toString(), "GET /api/uploads/u1" in node.calls)
        assertArrayEquals(File(folder, "mic.wav").readBytes(), node.uploads.getValue("u1").data)
        assertEquals(listOf("uploads/u1/mic.m4a"), node.submitted)
    }

    @Test
    fun lastChunkStillBeingWrittenIsWaitedForNotSentAgain() {
        val dir = recording(seconds = LONG_S)
        node.conflictOnLast = true
        node.writing = true

        val (first, _) = step(dir)

        assertEquals(StepResult.Again(30_000), first)
        assertEquals(listOf(0L, CHUNK, 2 * CHUNK), node.attempts)
        assertEquals(node.uploads.getValue("u1").size, ProcessStateFile.read(dir)!!.tracks.getValue("mic").uploaded)

        node.writing = false
        val (second, _) = step(dir)

        assertEquals(StepResult.Done, second)
        assertEquals(1, node.calls.count { it == "POST /api/uploads" })
        assertEquals(listOf(0L, CHUNK, 2 * CHUNK), node.attempts)
        assertEquals(listOf("uploads/u1/mic.m4a"), node.submitted)
    }

    @Test
    fun wholeUploadStillBeingWrittenIsWaitedFor() {
        val dir = recording(seconds = LONG_S)
        node.dropAfter = 3
        step(dir)

        node.down = false
        ProcessStateFile.write(dir, queued(ProcessStateFile.read(dir), false, id = recordingId(dir)!!, since = outcomeKey(Meta.read(dir))))
        node.writing = true
        val (second, _) = step(dir)

        assertEquals(StepResult.Again(30_000), second)
        assertEquals(1, node.calls.count { it == "POST /api/uploads" })

        node.writing = false
        val (third, _) = step(dir)

        assertEquals(StepResult.Done, third)
        assertEquals(1, node.calls.count { it == "POST /api/uploads" })
        assertEquals(listOf(0L, CHUNK, 2 * CHUNK), node.puts)
        assertEquals(listOf("uploads/u1/mic.m4a"), node.submitted)
    }

    @Test
    fun stalledChunkContinuesFromWhatReachedTheNode() {
        val dir = recording(seconds = LONG_S)
        node.stallAt = CHUNK
        // without OkHttp retries: 408 reaches the processing
        val once = http.newBuilder().retryOnConnectionFailure(false).build()

        val (outcome, folder) = step(dir, http = once)

        assertEquals(StepResult.Done, outcome)
        assertEquals(listOf(0L, CHUNK, CHUNK + CHUNK / 2), node.attempts)
        assertEquals(1, node.calls.count { it == "POST /api/uploads" })
        assertArrayEquals(File(folder, "mic.wav").readBytes(), node.uploads.getValue("u1").data)
        assertEquals(listOf("uploads/u1/mic.m4a"), node.submitted)
    }

    @Test
    fun expiredUploadStartsANewOne() {
        val dir = recording(seconds = LONG_S)
        node.dropAfter = 1
        step(dir)

        node.uploads.clear()
        node.down = false
        ProcessStateFile.write(dir, queued(ProcessStateFile.read(dir), false, id = recordingId(dir)!!, since = outcomeKey(Meta.read(dir))))
        node.puts.clear()
        val (outcome, folder) = step(dir)

        assertEquals(StepResult.Done, outcome)
        assertEquals(
            listOf("POST /api/uploads", "GET /api/uploads/u1", "POST /api/uploads"),
            node.calls.filter { it.startsWith("POST /api/uploads") || it.startsWith("GET /api/uploads") },
        )
        assertEquals(listOf(0L, CHUNK, 2 * CHUNK), node.puts)
        assertArrayEquals(File(folder, "mic.wav").readBytes(), node.uploads.getValue("u2").data)
        assertEquals(listOf("uploads/u2/mic.m4a"), node.submitted)
    }

    @Test
    fun uploadThatExpiresMidwayStartsOver() {
        val dir = recording(seconds = LONG_S)
        node.forgetAfter = 1

        val (outcome, folder) = step(dir)

        assertEquals(StepResult.Done, outcome)
        assertEquals(2, node.calls.count { it == "POST /api/uploads" })
        assertEquals(listOf(0L, 0L, CHUNK, 2 * CHUNK), node.puts)
        assertArrayEquals(File(folder, "mic.wav").readBytes(), node.uploads.getValue("u2").data)
        assertEquals(listOf("uploads/u2/mic.m4a"), node.submitted)
    }

    @Test
    fun uploadProgressCountsTheBytesOfBothTracks() {
        step(recording(system = true))

        val sent = progress.filterIsInstance<ProcessingState.Uploading>().map { it.progress!! }
        assertEquals(0.0, sent.first(), 0.0)
        assertEquals(1.0, sent.last(), 0.0)
        assertTrue(sent.toString(), 0.5 in sent)
        assertEquals(sent.sorted(), sent)
    }

    @Test
    fun secondTrackIsCompressedByTheNextStepWhenTimeRunsShort() {
        val dir = recording(system = true)
        val encoder = CopyEncoder()
        encodeMs = 5 * MINUTE_MS

        val (first, _) = step(dir, encoder = encoder)

        assertEquals(StepResult.Again(1_000), first)
        assertEquals(listOf("mic.wav"), encoder.encoded)
        assertEquals(emptyList<String>(), node.calls)

        val (second, _) = step(dir, encoder = encoder)

        assertEquals(StepResult.Done, second)
        assertEquals(listOf("mic.wav", "system.wav"), encoder.encoded)
        assertEquals(2, node.submitted.size)
    }

    @Test
    fun stoppedCompressionLeavesNoHalfFile() {
        val dir = recording()
        val started = CountDownLatch(1)
        val blocking = object : AudioEncoder {
            override fun encode(wav: File, out: File) = encodeInto(out) { partial ->
                partial.writeBytes(ByteArray(1000))
                started.countDown()
                Thread.sleep(60_000)
            }
        }

        runBlocking {
            val work = launch(Dispatchers.IO) { pipeline(blocking, http).step(dir, now + STEP_MS) {} }
            withContext(Dispatchers.IO) { assertTrue(started.await(10, TimeUnit.SECONDS)) }
            work.cancelAndJoin()
        }

        assertEquals(emptyList<String>(), cache.list().orEmpty().toList())
        assertEquals(emptyList<String>(), node.calls)
        assertEquals(StepResult.Done, step(dir).first)
    }

    // compression is a copy; it "takes" encodeMs by the processing clock
    private inner class CopyEncoder : AudioEncoder {
        val encoded = CopyOnWriteArrayList<String>()

        override fun encode(wav: File, out: File) {
            encoded += wav.name
            now += encodeMs
            wav.copyTo(out, overwrite = true)
        }
    }

    private class Upload(val name: String, val size: Long) {
        var data = ByteArray(0)

        fun path(id: String): String? = if (data.size.toLong() == size) "uploads/$id/$name" else null

        fun pathJson(id: String): String = path(id)?.let { "\"$it\"" } ?: "null"
    }

    // a node with chunked upload; dropAfter — after this many accepted chunks the connection drops and the last answer is lost
    private inner class Node : Dispatcher() {
        var down = false
        var dropAfter = -1
        var conflictAt: Long? = null

        // a repeat of the last chunk: the node accepted everything and answers 409 with the offset at the end of the file
        var conflictOnLast = false

        // the last chunk is still being written: the file is whole, no path yet
        var writing = false

        // a chunk from this offset goes silent halfway: 408 with the node's offset
        var stallAt: Long? = null

        // after this many accepted chunks the upload expires
        var forgetAfter = -1
        val calls = CopyOnWriteArrayList<String>()
        val puts = CopyOnWriteArrayList<Long>()

        // offsets of all PUTs, unaccepted too
        val attempts = CopyOnWriteArrayList<Long>()
        val uploads = ConcurrentHashMap<String, Upload>()
        val submitted = CopyOnWriteArrayList<String>()
        private var created = 0
        private var stored = 0
        private var jobs = 0

        @Synchronized
        override fun dispatch(request: RecordedRequest): MockResponse {
            if (down) return dropped()
            val path = request.url.encodedPath
            calls += "${request.method} $path"
            val id = path.removePrefix("/api/uploads/")
            return when {
                request.method == "POST" && path == "/api/uploads" -> create(request)
                request.method == "GET" && path.startsWith("/api/uploads/") -> uploads[id]?.let { reply(200, it.json(id)) } ?: reply(404, "{}")
                request.method == "PUT" && path.startsWith("/api/uploads/") -> put(id, request)
                request.method == "POST" && path == "/api/jobs" -> {
                    submitted += Json.parseToJsonElement(request.body!!.utf8()).jsonObject["tracks"]!!.jsonObject.values.map { it.jsonPrimitive.content }
                    reply(200, """{"id":"job-${++jobs}"}""")
                }
                request.method == "GET" && path.startsWith("/api/jobs/") -> reply(200, DONE_JOB)
                else -> reply(404, "{}")
            }
        }

        private fun create(request: RecordedRequest): MockResponse {
            val body = Json.parseToJsonElement(request.body!!.utf8()).jsonObject
            val id = "u${++created}"
            uploads[id] = Upload(body["name"]!!.jsonPrimitive.content, body["size"]!!.jsonPrimitive.long)
            return reply(201, """{"id":"$id","offset":0}""")
        }

        private fun put(id: String, request: RecordedRequest): MockResponse {
            val upload = uploads[id] ?: return reply(404, "{}")
            val offset = request.url.queryParameter("offset")!!.toLong()
            attempts += offset
            if (offset == conflictAt) {
                conflictAt = null
                // the node lost the previous chunk
                upload.data = upload.data.copyOf((offset - CHUNK).toInt())
                return reply(409, """{"detail":"Смещение не совпадает","offset":${upload.data.size}}""")
            }
            if (offset != upload.data.size.toLong()) return reply(409, """{"detail":"Смещение не совпадает","offset":${upload.data.size}}""")
            val body = request.body!!.toByteArray()
            if (offset == stallAt) {
                stallAt = null
                upload.data += body.copyOf(body.size / 2)
                return reply(408, """{"detail":"Кусок дошёл не целиком","offset":${upload.data.size}}""")
            }
            upload.data += body
            puts += offset
            if (conflictOnLast && upload.path(id) != null) {
                return reply(409, """{"detail":"Узел ждёт кусок со смещения ${upload.size}","offset":${upload.size}}""")
            }
            if (stored + 1 == forgetAfter) uploads.remove(id)
            if (++stored == dropAfter) {
                down = true
                return dropped()
            }
            return reply(200, """{"offset":${upload.data.size},"path":${upload.pathJson(id)}}""")
        }

        private fun Upload.json(id: String) =
            """{"id":"$id","name":"$name","size":$size,"offset":${data.size},"path":${if (writing) "null" else pathJson(id)}}"""

        private fun reply(code: Int, body: String) = MockResponse(code, body = body)

        private fun dropped() = MockResponse.Builder().onResponseStart(SocketEffect.CloseSocket()).build()
    }

    private companion object {
        const val START_MS = 1_800_000_000_000L
        const val MINUTE_MS = 60_000L
        const val STEP_MS = 8 * MINUTE_MS
        const val CHUNK = 4L * 1024 * 1024

        // 300 s of PCM16 16 kHz is 9.6 MB: three chunks
        const val LONG_S = 300.0
        const val DONE_JOB =
            """{"id":"j","status":"done","result":{"files":{"transcript.txt":"mic: Привет"},"run":{"status":"ok"}}}"""
    }
}
