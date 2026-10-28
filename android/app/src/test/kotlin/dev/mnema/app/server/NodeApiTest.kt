package dev.mnema.app.server

import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.jsonObject
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

class NodeApiTest {
    private val server = MockWebServer()
    private val http: OkHttpClient = RunnerClient.defaultHttp()
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

    private fun client(token: String? = "device-token") =
        RunnerClient(Connection(server.url("/").toString().trimEnd('/'), token), http)

    private fun reply(code: Int, body: String = "") = MockResponse.Builder().code(code).body(body).build()

    private fun RecordedRequest.json(): Map<String, JsonElement> = Json.parseToJsonElement(body!!.utf8()).jsonObject

    private inline fun <reified E : Exception> failure(crossinline block: suspend () -> Unit): E {
        try {
            runBlocking { block() }
        } catch (e: Exception) {
            if (e is E) return e
            throw e
        }
        fail("no ${E::class.simpleName}")
        throw AssertionError()
    }

    @Test
    fun claimSendsTheCodeAndDeviceNameWithoutATokenAndGivesTheDeviceToken() = runBlocking<Unit> {
        server.enqueue(reply(200, """{"token":"dev-tok_1","device":{"id":"d1","name":"Pixel 9"}}"""))

        assertEquals("dev-tok_1", client(token = null).claimInvite("invite-code", "Pixel 9"))

        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/devices/claim", request.target)
        assertNull(request.headers["Authorization"])
        assertEquals(mapOf("invite" to "invite-code", "name" to "Pixel 9"), request.json().mapValues { it.value.toString().trim('"') })
    }

    @Test
    fun refusedInviteGivesTheNodeTextWithoutTheCode() {
        server.enqueue(reply(403, """{"detail":"Приглашение недействительно: попросите новое на узле"}"""))

        val error = failure<RunnerException> { client(token = null).claimInvite("invite-code", "Pixel 9") }

        assertEquals("Приглашение недействительно: попросите новое на узле", error.message)
        assertFalse("invite-code" in error.message.orEmpty())
    }

    @Test
    fun claimWithoutATokenInTheAnswerIsABadResponse() {
        server.enqueue(reply(200, """{"device":{"id":"d1"}}"""))

        assertEquals(RunnerErrorKind.BAD_RESPONSE, failure<RunnerException> { client(null).claimInvite("c", "Pixel") }.kind)
    }

    @Test
    fun capabilitiesTellWhatTheNodeCanDoAndItsName() = runBlocking<Unit> {
        server.enqueue(reply(200, """{"ok":true,"llm":true,"name":"Сервер"}"""))
        server.enqueue(reply(200, """{"ok":true,"llm":false}"""))

        assertEquals(NodeCheck(Health.Ok, llm = true, name = "Сервер"), client().capabilities())
        assertEquals(NodeCheck(Health.Ok, llm = false, name = null), client().capabilities())
        assertEquals("Bearer device-token", server.takeRequest().headers["Authorization"])
    }

    @Test
    fun capabilitiesOfAProblemNodeCarryOnlyTheProblem() = runBlocking<Unit> {
        server.enqueue(reply(401))
        server.enqueue(reply(404))
        server.enqueue(reply(200, "<html></html>"))

        assertEquals(NodeCheck(Health.TokenRejected), client().capabilities())
        assertEquals(NodeCheck(Health.NotMnema), client().capabilities())
        assertEquals(NodeCheck(Health.NotMnema), client().capabilities())
        val stopped = MockWebServer()
        stopped.start()
        val url = stopped.url("/").toString().trimEnd('/')
        stopped.close()
        assertEquals(NodeCheck(Health.Unreachable), RunnerClient(Connection(url, "t"), http).capabilities())
    }

    @Test
    fun rejectedTokenIsATokenError() {
        server.enqueue(reply(401))

        assertEquals(RunnerErrorKind.TOKEN, failure<RunnerException> { client().nodeSettings() }.kind)
    }
    @Test
    fun nodeSettingsReadOnlyContractUsesTheCanonicalModelString() = runBlocking<Unit> {
        server.enqueue(reply(200, """{"name":"ПК Ubuntu","model":"gemini/gemini-flash-latest","engines":true}"""))
        server.enqueue(reply(200, """{"name":"Узел","model":null,"engines":false}"""))
        assertEquals(NodeSettings("ПК Ubuntu", "gemini/gemini-flash-latest", true), client().nodeSettings())
        assertEquals("a node without a model", NodeSettings("Узел", null, false), client().nodeSettings())
        val request = server.takeRequest()
        assertEquals("GET", request.method)
        assertEquals("/api/node/settings", request.target)
        assertEquals("Bearer device-token", request.headers["Authorization"])
        assertEquals(0L, request.bodySize)
        assertEquals(2, server.requestCount)
    }
}
