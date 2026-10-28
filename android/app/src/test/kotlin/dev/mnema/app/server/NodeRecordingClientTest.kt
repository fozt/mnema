package dev.mnema.app.server

import java.net.ConnectException
import java.net.InetAddress
import java.net.Socket
import javax.net.SocketFactory
import dev.mnema.app.sync.resource
import kotlinx.coroutines.runBlocking
import mockwebserver3.SocketEffect
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.Assert.*
import org.junit.Test

class NodeRecordingClientTest {
    private val strict = Json { encodeDefaults = true }
    private fun contract() = Json.parseToJsonElement(resource("recording-api-v1.json")).jsonObject

    private fun withNode(token: String? = "token", block: suspend (MockWebServer, RunnerClient) -> Unit) = runBlocking {
        val http = RunnerClient.defaultHttp()
        try {
            MockWebServer().use { server ->
                server.start()
                block(server, RunnerClient(Connection(server.url("/").toString().trimEnd('/'), token), http))
            }
        } finally {
            http.dispatcher.executorService.shutdown()
            http.connectionPool.evictAll()
        }
    }

    @Test fun requestsAreSentAsInTheSharedContract() = withNode { server, client ->
        for (request in contract().getValue("requests").jsonArray.map { it.jsonObject }) {
            server.enqueue(MockResponse.Builder().body("""{"id":"job"}""").build())
            val key = request.getValue("key").jsonPrimitive.content
            val params = request.getValue("params")
            val id = if (request.getValue("op").jsonPrimitive.content == "recording") {
                val tracks = request.getValue("tracks").jsonObject.mapValues { it.value.jsonPrimitive.content }
                client.submitRecording(key, tracks, strict.decodeFromJsonElement(RecordingParams.serializer(), params))
            } else {
                val path = request.getValue("path").jsonPrimitive.content
                client.submitRecordingSummary(key, path, strict.decodeFromJsonElement(SummaryParams.serializer(), params))
            }
            assertEquals("job", id)
            val sent = server.takeRequest()
            assertEquals("POST", sent.method)
            assertEquals("/api/jobs", sent.target)
            assertEquals("Bearer token", sent.headers["Authorization"])
            assertEquals(request, Json.parseToJsonElement(sent.body!!.utf8()))
        }
    }

    @Test fun jobAnswersAreReadAsInTheSharedContract() = withNode { server, client ->
        for (case in contract().getValue("jobs").jsonArray.map { it.jsonObject }) {
            val body = case.getValue("body")
            server.enqueue(MockResponse.Builder().body(body.toString()).build())
            val job = requireNotNull(client.nodeJob("job"))
            assertEquals(body, strict.encodeToJsonElement(NodeJob.serializer(), job))
        }
        server.enqueue(MockResponse.Builder().code(404).body("""{"detail":"Задача не найдена"}""").build())
        assertNull(client.nodeJob("job"))
    }

    @Test fun missingUploadNamesItsTrackAndForgetToleratesAMissingJob() = withNode { server, client ->
        val params = RecordingParams(started = "2026-10-22T00:30:00+03:00", name = "2026-10-22_00-30", asr = "auto")
        server.enqueue(MockResponse.Builder().code(404).body("""{"detail":"нет загрузки","track":"system"}""").build())
        val failed = try { client.submitRecording("retry", emptyMap(), params); null } catch (e: RunnerException) { e }
        assertEquals(RunnerErrorKind.UPLOAD_GONE, failed?.kind)
        assertEquals("system", failed?.track)
        server.enqueue(MockResponse.Builder().code(409).body("""{"detail":"Ключ повтора занят другой задачей","reason":"key_taken"}""").build())
        val taken = try { client.submitRecording("retry", emptyMap(), params); null } catch (e: RunnerException) { e }
        assertEquals(RunnerErrorKind.KEY_TAKEN, taken?.kind)
        server.enqueue(MockResponse.Builder().code(404).build())
        client.forget("job")
        server.enqueue(MockResponse.Builder().body("""{"choices":[{"id":"auto","title":"Автоматически"}]}""").build())
        assertEquals(listOf(AsrOption("auto", "Автоматически")), client.asrChoices())
    }

    @Test fun validationErrorsRemainReadable() = withNode(token = null) { server, client ->
        val params = SummaryParams("2026-10-22T16:01:00+03:00", "Имя", "ru")
        for (code in listOf(400, 422)) {
            server.enqueue(MockResponse.Builder().code(code).body("""{"detail":"Выберите модель узла"}""").build())
            val error = try { client.submitRecordingSummary("key", "path", params); null } catch (e: RunnerException) { e }
            assertEquals(RunnerErrorKind.JOB_FAILED, error?.kind)
            assertEquals("Выберите модель узла", error?.message)
        }
    }

    @Test fun lostReplyIsMarkedOnlyAfterRequestBodyWasSent() = runBlocking {
        val http = RunnerClient.defaultHttp().newBuilder().retryOnConnectionFailure(false).build()
        try {
            MockWebServer().use { server ->
                server.start()
                val client = RunnerClient(Connection(server.url("/").toString().trimEnd('/'), null), http)
                val params = RecordingParams("2026-10-22T16:01:00+03:00", "Запись")
                server.enqueue(MockResponse.Builder().onResponseStart(SocketEffect.CloseSocket()).build())
                val lost = try { client.submitRecording("same", emptyMap(), params); null } catch (e: RunnerException) { e }
                assertEquals(RunnerErrorKind.UNREACHABLE, lost?.kind)
                assertEquals(true, lost?.requestSent)
                assertEquals(1, server.requestCount)
                server.enqueue(MockResponse(503, body = "{}"))
                val down = try { client.submitRecording("same", emptyMap(), params); null } catch (e: RunnerException) { e }
                assertEquals(false, down?.requestSent)
                server.enqueue(MockResponse.Builder().onResponseStart(SocketEffect.CloseSocket()).build())
                val poll = try { client.nodeJob("j"); null } catch (e: RunnerException) { e }
                assertEquals(false, poll?.requestSent)
            }
        } finally { http.dispatcher.executorService.shutdown(); http.connectionPool.evictAll() }
    }

    @Test fun failedConnectionNeverCountsAsSubmitted() = runBlocking {
        var connections = 0
        val sockets = object : SocketFactory() {
            override fun createSocket(): Socket { connections++; throw ConnectException("refused") }
            override fun createSocket(host: String, port: Int): Socket = error("unused")
            override fun createSocket(host: String, port: Int, local: InetAddress, localPort: Int): Socket = error("unused")
            override fun createSocket(host: InetAddress, port: Int): Socket = error("unused")
            override fun createSocket(host: InetAddress, port: Int, local: InetAddress, localPort: Int): Socket = error("unused")
        }
        val http = RunnerClient.defaultHttp().newBuilder().socketFactory(sockets).build()
        try {
            val client = RunnerClient(Connection("http://127.0.0.1:1234", null), http)
            val error = try { client.submitRecording("same", emptyMap(), RecordingParams("start", "name")); null }
                catch (e: RunnerException) { e }
            assertEquals(RunnerErrorKind.UNREACHABLE, error?.kind)
            assertEquals(false, error?.requestSent)
            assertEquals(1, connections)
        } finally { http.dispatcher.executorService.shutdown(); http.connectionPool.evictAll() }
    }

}
