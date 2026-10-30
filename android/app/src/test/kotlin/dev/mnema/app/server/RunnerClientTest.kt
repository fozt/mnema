package dev.mnema.app.server

import dev.mnema.app.i18n.Ui
import dev.mnema.app.i18n.UiLang
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class RunnerClientTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val server = MockWebServer()
    private val http: OkHttpClient = RunnerClient.defaultHttp()
    private val capabilities = """{"ok":true,"llm":false}"""

    @Before
    fun setUp() {
        server.start()
    }

    @After
    fun tearDown() {
        server.close()
        http.dispatcher.executorService.shutdown()
        http.connectionPool.evictAll()
    }

    private fun connection(token: String? = null, path: String = ""): Connection =
        Connection(server.url("/").toString().trimEnd('/') + path, token)

    private fun client(token: String? = null, path: String = "") = RunnerClient(connection(token, path), http)

    private fun reply(code: Int, body: String = "") = MockResponse.Builder().code(code).body(body).build()

    private fun failure(block: suspend () -> Unit): RunnerException {
        try {
            runBlocking { block() }
        } catch (e: RunnerException) {
            return e
        }
        fail("no RunnerException")
        throw AssertionError()
    }

    @Test
    fun healthOfAMnemaServerIsOkAndSendsTheBearerToken() = runBlocking<Unit> {
        server.enqueue(reply(200, capabilities))
        server.enqueue(reply(200, capabilities))

        assertEquals(Health.Ok, client("t").capabilities().health)
        assertEquals(Health.Ok, client().capabilities().health)

        val request = server.takeRequest()
        assertEquals("GET", request.method)
        assertEquals("/api/capabilities", request.target)
        assertEquals("Bearer t", request.headers["Authorization"])
        assertNull("no header without a token", server.takeRequest().headers["Authorization"])
    }

    @Test
    fun everyAnswerWithANameRenamesTheNode() = runBlocking<Unit> {
        val names = mutableListOf<String>()
        val client = RunnerClient(Connection(server.url("/").toString().trimEnd('/'), null), http) { names += it }
        server.enqueue(reply(200, """{"ok":true,"llm":false,"name":" Ноутбук "}"""))
        server.enqueue(reply(200, """{"ok":true,"llm":false}"""))
        server.enqueue(reply(401))

        assertEquals("Ноутбук", client.capabilities().name)
        client.capabilities()
        client.capabilities()

        assertEquals(listOf("Ноутбук"), names)
    }

    @Test
    fun connectionLineTokenReachesTheServerUnchangedAndOutOfTheUrl() = runBlocking<Unit> {
        val token = "secret-#-%-&-+-'-\\-\$HOME"
        server.enqueue(reply(200, capabilities))
        val line = formatConnection(server.url("/").toString(), token)
        val parsed = parseConnection(line)

        assertEquals(Health.Ok, RunnerClient(parsed, http).capabilities().health)

        val request = server.takeRequest()
        assertEquals("Bearer $token", request.headers["Authorization"])
        assertEquals("/api/capabilities", request.target)
        assertNull(request.url.fragment)
        assertNull(request.url.query)
    }

    @Test
    fun serverUnderAPathPrefixIsAddressedThroughIt() = runBlocking<Unit> {
        server.enqueue(reply(200, capabilities))

        assertEquals(Health.Ok, client(path = "/mnema").capabilities().health)

        assertEquals("/mnema/api/capabilities", server.takeRequest().target)
    }

    @Test
    fun healthGivesUpAfterTheTimeout() = runBlocking<Unit> {
        server.enqueue(MockResponse.Builder().code(200).body(capabilities).headersDelay(3, TimeUnit.SECONDS).build())
        val started = System.nanoTime()

        assertEquals(Health.Unreachable, client().capabilities(timeoutMs = 300).health)

        assertTrue(System.nanoTime() - started < TimeUnit.SECONDS.toNanos(2))
    }

    @Test
    fun healthDistinguishesMissingAndRejectedTokens() = runBlocking<Unit> {
        server.enqueue(reply(401))
        server.enqueue(reply(401))
        server.enqueue(reply(403))

        assertEquals(Health.TokenRequired, client().capabilities().health)
        assertEquals(Health.TokenRejected, client("wrong").capabilities().health)
        assertEquals(Health.TokenRejected, client("wrong").capabilities().health)
    }

    @Test
    fun healthDoesNotFollowRedirectsAndDoesNotLeakTheToken() = runBlocking<Unit> {
        MockWebServer().use { other ->
            other.start()
            server.enqueue(
                MockResponse.Builder().code(302).addHeader("Location", other.url("/api/capabilities").toString()).build(),
            )
            other.enqueue(reply(200, capabilities))

            assertEquals(Health.Unreachable, client("t").capabilities().health)

            assertEquals(0, other.requestCount)
        }
    }

    @Test
    fun startUploadPostsNameAndSizeAndReturnsTheId() = runBlocking<Unit> {
        server.enqueue(reply(201, """{"id":"ab12","offset":0}"""))

        assertEquals("ab12", client("t").startUpload("mic.m4a", 12_345))

        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/uploads", request.target)
        assertEquals("Bearer t", request.headers["Authorization"])
        assertTrue(request.headers["Content-Type"]!!.startsWith("application/json"))
        assertEquals("""{"name":"mic.m4a","size":12345}""", request.body!!.utf8())
    }

    @Test
    fun startUploadMapsServerAnswersToErrorKinds() {
        val answers = listOf(
            reply(401) to RunnerErrorKind.TOKEN,
            reply(503) to RunnerErrorKind.UNREACHABLE,
            reply(429) to RunnerErrorKind.QUEUE_FULL,
            reply(422) to RunnerErrorKind.BAD_RESPONSE,
            reply(201, """{"offset":0}""") to RunnerErrorKind.BAD_RESPONSE,
            reply(404, """{"detail":"Not Found"}""") to RunnerErrorKind.BAD_RESPONSE,
            reply(405, """{"detail":"Method Not Allowed"}""") to RunnerErrorKind.BAD_RESPONSE,
        )
        answers.forEach { server.enqueue(it.first) }

        val kinds = List(answers.size) { failure { client("t").startUpload("mic.m4a", 10) }.kind }

        assertEquals(answers.map { it.second }, kinds)
    }

    @Test
    fun uploadOffsetIsTheNodeStateOrNullForAnExpiredUpload() = runBlocking<Unit> {
        server.enqueue(reply(200, """{"id":"ab","name":"mic.m4a","size":10,"offset":4,"path":null}"""))
        server.enqueue(reply(200, """{"id":"ab","name":"mic.m4a","size":10,"offset":10,"path":"/data/uploads/ab/mic.m4a"}"""))
        server.enqueue(reply(404, """{"detail":"Загрузки нет"}"""))

        assertEquals(UploadInfo(offset = 4, name = "mic.m4a", size = 10), client("t").uploadOffset("ab"))
        assertEquals("/data/uploads/ab/mic.m4a", client("t").uploadOffset("ab")!!.path)
        assertNull(client("t").uploadOffset("ab"))

        val request = server.takeRequest()
        assertEquals("GET", request.method)
        assertEquals("/api/uploads/ab", request.target)
        assertEquals("Bearer t", request.headers["Authorization"])
    }

    @Test
    fun uploadChunkPutsOnlyTheCountedBytesAtTheOffset() = runBlocking<Unit> {
        server.enqueue(reply(200, """{"offset":7,"path":null}"""))
        server.enqueue(reply(200, """{"offset":10,"path":"/data/uploads/ab/mic.m4a"}"""))

        val info = client("t").uploadChunk("ab", 4, "0123456789".toByteArray(), 3)
        val last = client("t").uploadChunk("ab", 7, ByteArray(3), 3)

        assertEquals(UploadInfo(offset = 7), info)
        assertEquals("the last chunk", UploadInfo(offset = 10, path = "/data/uploads/ab/mic.m4a"), last)
        val request = server.takeRequest()
        assertEquals("PUT", request.method)
        assertEquals("/api/uploads/ab?offset=4", request.target)
        assertEquals("Bearer t", request.headers["Authorization"])
        assertEquals("application/octet-stream", request.headers["Content-Type"])
        assertEquals("012", request.body!!.utf8())
    }

    @Test
    fun conflictingChunkGivesTheOffsetOfTheNode() = runBlocking<Unit> {
        server.enqueue(reply(409, """{"detail":"Смещение не совпадает","offset":2}"""))

        assertEquals(UploadInfo(offset = 2), client().uploadChunk("ab", 4, ByteArray(3), 3))
    }

    @Test
    fun conflictWithoutAnOffsetAsksTheNodeForIt() = runBlocking<Unit> {
        server.enqueue(reply(409, """{"detail":"Загрузка занята"}"""))
        server.enqueue(reply(200, """{"id":"ab","name":"mic.m4a","size":10,"offset":5,"path":null}"""))

        assertEquals(5L, client().uploadChunk("ab", 4, ByteArray(3), 3).offset)

        assertEquals("PUT", server.takeRequest().method)
        assertEquals("GET /api/uploads/ab", server.takeRequest().let { "${it.method} ${it.target}" })
    }

    // every PUT stalls; OkHttp retries after 408 by itself, so every PUT gets the reply
    private fun stallingNode(stalled: String) = object : Dispatcher() {
        override fun dispatch(request: RecordedRequest) = when (request.method) {
            "PUT" -> reply(408, stalled)
            else -> reply(200, """{"id":"ab","name":"mic.m4a","size":10,"offset":5,"path":null}""")
        }
    }

    @Test
    fun stalledChunkGivesTheOffsetThatReachedTheNode() = runBlocking<Unit> {
        server.dispatcher = stallingNode("""{"detail":"Кусок дошёл не целиком","offset":6}""")

        assertEquals(UploadInfo(offset = 6), client().uploadChunk("ab", 4, ByteArray(3), 3))
        assertTrue(List(server.requestCount) { server.takeRequest().method }.all { it == "PUT" })
    }

    @Test
    fun stallWithoutAnOffsetAsksTheNodeForIt() = runBlocking<Unit> {
        server.dispatcher = stallingNode("<html>Request Timeout</html>")

        assertEquals(5L, client().uploadChunk("ab", 4, ByteArray(3), 3).offset)
        assertEquals("GET /api/uploads/ab", List(server.requestCount) { server.takeRequest() }.last().let { "${it.method} ${it.target}" })
    }

    @Test
    fun uploadChunkMapsServerAnswersToErrorKinds() {
        server.enqueue(reply(404))
        server.enqueue(reply(401))
        server.enqueue(reply(502))
        server.enqueue(reply(400, """{"detail":"Кусок за размером"}"""))
        server.enqueue(reply(200, """{"path":null}"""))

        val kinds = List(5) { failure { client("t").uploadChunk("ab", 0, ByteArray(1), 1) }.kind }

        assertEquals(
            listOf(
                RunnerErrorKind.UPLOAD_GONE,
                RunnerErrorKind.TOKEN,
                RunnerErrorKind.UNREACHABLE,
                RunnerErrorKind.BAD_RESPONSE,
                RunnerErrorKind.BAD_RESPONSE,
            ),
            kinds,
        )
    }

    @Test
    fun jobParsesProgressAndIgnoresServerOnlyFields() = runBlocking<Unit> {
        server.enqueue(
            reply(
                200,
                """{"id":"j1","op":"transcribe","path":"/u/a.m4a","url":null,"urls":null,"params":{"backend":"auto"},""" +
                    """"status":"running","created_at":1.5,"started_at":2.5,"finished_at":null,"progress":0.4,""" +
                    """"message":"Распознаю","result":null,"error":null}""",
            ),
        )
        val text = "Сервер перезапустился во время задачи. Запустите её снова"
        server.enqueue(reply(200, """{"id":"j1","status":"error","message":"","result":null,"error":"$text"}"""))

        val job = client("t").nodeJob("j1")
        val failed = requireNotNull(client("t").nodeJob("j1"))

        val request = server.takeRequest()
        assertEquals("GET", request.method)
        assertEquals("/api/jobs/j1", request.target)
        assertEquals(NodeJob(id = "j1", status = "running", progress = 0.4, message = "Распознаю"), job)
        assertEquals("error", failed.status)
        assertEquals(text, failed.error)
    }

    @Test
    fun jobMapsServerAnswersToErrorKinds() {
        server.enqueue(reply(404))
        server.enqueue(reply(401))
        server.enqueue(reply(504))
        server.enqueue(reply(200, """{"id":"j1"}"""))
        server.enqueue(reply(200, """{"id":"j1","status":"done","result":{"files":"no"}}"""))

        assertNull(runBlocking { client("t").nodeJob("j1") })
        assertEquals(RunnerErrorKind.TOKEN, failure { client("t").nodeJob("j1") }.kind)
        assertEquals(RunnerErrorKind.UNREACHABLE, failure { client("t").nodeJob("j1") }.kind)
        assertEquals(RunnerErrorKind.BAD_RESPONSE, failure { client("t").nodeJob("j1") }.kind)
        assertEquals(RunnerErrorKind.BAD_RESPONSE, failure { client("t").nodeJob("j1") }.kind)
    }

    @Test
    fun errorsAndHealthAreEnglishInAnEnglishInterface() {
        val before = Ui.lang
        Ui.lang = UiLang.EN
        try {
            server.enqueue(reply(500))
            server.enqueue(reply(429))
            server.enqueue(reply(401))
            assertEquals("The server returned error 500", failure { client("t").nodeJob("j1") }.message)
            assertEquals("The server is busy: the queue is full. Try again later", failure { client("t").nodeJob("j1") }.message)
            assertEquals("Paste the invitation from the server", failure { client().nodeJob("j1") }.message)
            val messages = listOf(Health.Unreachable, Health.TokenRequired, Health.TokenRejected, Health.NotMnema).map { it.message()!! }
            assertEquals("A new invitation from the server is needed", Health.TokenRejected.message())
            messages.forEach { assertFalse(it, it.any { c -> c in 'а'..'я' || c in 'А'..'Я' }) }
            val refused = try { parseConnection("http://host#token") ; null } catch (e: ConnectionException) { e.message }
            assertEquals("Invalid invitation. Copy the whole invitation from the server", refused)
        } finally { Ui.lang = before }
    }

    @Test
    fun jobIdIsAnEscapedPathSegment() = runBlocking<Unit> {
        server.enqueue(reply(200, """{"id":"x","status":"pending"}"""))

        client().nodeJob("a/b c")

        assertEquals("/api/jobs/a%2Fb%20c", server.takeRequest().target)
    }

    @Test
    fun cancelPostsToTheJobAndToleratesAFinishedOrUnknownJob() = runBlocking<Unit> {
        server.enqueue(reply(200, """{"ok":true,"status":"running"}"""))
        server.enqueue(reply(200, """{"ok":false,"status":"done"}"""))
        server.enqueue(reply(404))

        client("t").cancel("j1")
        client("t").cancel("j1")
        client("t").cancel("gone")

        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/jobs/j1/cancel", request.target)
        assertEquals("Bearer t", request.headers["Authorization"])
    }

    @Test
    fun cancelReportsTokenAndTransportProblems() {
        server.enqueue(reply(401))
        server.enqueue(reply(500))

        assertEquals(RunnerErrorKind.TOKEN, failure { client().cancel("j1") }.kind)
        assertEquals(RunnerErrorKind.BAD_RESPONSE, failure { client().cancel("j1") }.kind)
    }

    @Test
    fun cancellingTheCoroutineAbortsTheHttpCall() = runBlocking<Unit> {
        server.enqueue(MockResponse.Builder().code(200).body("{}").headersDelay(3, TimeUnit.SECONDS).build())
        val pending = launch(Dispatchers.IO) { runCatching { client().nodeJob("j1") } }
        assertNotNull(server.takeRequest(5, TimeUnit.SECONDS))

        pending.cancelAndJoin()

        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2)
        while (http.dispatcher.runningCallsCount() > 0 && System.nanoTime() < deadline) delay(20)
        assertEquals(0, http.dispatcher.runningCallsCount())
    }
}
