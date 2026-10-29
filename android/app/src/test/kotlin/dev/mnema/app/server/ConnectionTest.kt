package dev.mnema.app.server

import dev.mnema.app.sync.resource
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/** Parsing and building a connection string and "is this a Mnema server", from fixture connection-v1.json shared with the desktop. */
class ConnectionTest {
    private val fixture = Json.parseToJsonElement(resource("connection-v1.json")).jsonObject
    private val messages = fixture.getValue("messages").jsonObject

    private fun cases(name: String): List<JsonObject> = fixture.getValue(name).jsonArray.map { it.jsonObject }

    private fun JsonObject.text(name: String): String? = (get(name) as? JsonPrimitive)?.contentOrNull

    private fun message(case: JsonObject): String = messages.getValue(case.text("error")!!).jsonPrimitive.content

    private fun rejected(block: () -> Unit): ConnectionException {
        try {
            block()
        } catch (e: ConnectionException) {
            return e
        }
        fail("accepted")
        throw AssertionError()
    }

    @Test
    fun connectionStringIsReadAsOnTheDesktop() {
        for (case in cases("parse")) {
            val text = case.text("text")!!
            if (case.text("error") != null) {
                assertEquals(text, message(case), rejected { parseConnection(text) }.message)
                continue
            }
            val parsed = parseConnection(text)
            assertEquals(text, case.text("url"), parsed.url)
            assertEquals(text, case.text("token"), parsed.token)
            assertEquals(text, case.text("invite"), parsed.invite)
        }
    }

    @Test
    fun connectionStringIsMadeAsOnTheDesktop() {
        for (case in cases("format")) {
            val url = case.text("url")!!
            val token = case.text("token")
            if (case.text("error") != null) {
                assertEquals(url, message(case), rejected { formatConnection(url, token) }.message)
                continue
            }
            val line = formatConnection(url, token)
            assertEquals(url, case.text("line"), line)
            assertEquals(url, token?.ifEmpty { null }, parseConnection(line).token)
        }
    }

    @Test
    fun mnemaServerIsRecognisedAsOnTheDesktop() = runBlocking<Unit> {
        val http = RunnerClient.defaultHttp()
        MockWebServer().use { server ->
            server.start()
            val client = RunnerClient(Connection(server.url("/").toString().trimEnd('/'), null), http)
            for (case in cases("capabilities")) {
                val status = case.getValue("status").jsonPrimitive.int
                val body = case.text("body")!!
                server.enqueue(MockResponse.Builder().code(status).body(body).build())
                val expected = when (case.text("answer")) {
                    "mnema" -> Health.Ok
                    "not_mnema" -> Health.NotMnema
                    else -> Health.Unreachable
                }
                val check = client.capabilities()
                assertEquals("$status $body", expected, check.health)
                if (expected == Health.Ok) {
                    val version = (Json.parseToJsonElement(body).jsonObject["version"] as? JsonPrimitive)?.contentOrNull
                    assertEquals("$status $body", version, check.version)
                }
            }
        }
        http.dispatcher.executorService.shutdown()
        http.connectionPool.evictAll()
    }

    @Test
    fun versionsDifferingInMajorMinorSayWhichSideToUpdateAsOnTheDesktop() {
        for (case in cases("versions")) {
            val server = (case["server"] as? JsonPrimitive)?.takeIf { it.isString }?.content
            val expected = when (case.text("gap")) {
                "app" -> VersionGap.APP
                "server" -> VersionGap.SERVER
                else -> null
            }
            assertEquals("$case", expected, versionGap(server, case.text("app")!!))
        }
        assertEquals("На сервере новая версия Mnema 0.4.0. Обновите приложение", versionNotice("0.4.0", "0.3.1"))
        assertEquals("На сервере старая версия Mnema 0.2.5. Обновите сервер", versionNotice("0.2.5", "0.3.1"))
        assertEquals(null, versionNotice("0.3.9", "0.3.1"))
    }

    @Test
    fun connectionToStringHidesTheToken() {
        val text = Connection("http://host", "very-secret").toString()
        assertTrue(text.contains("http://host"))
        assertFalse(text.contains("very-secret"))
        assertFalse(Connection("http://host", null, "invite-code").toString().contains("invite-code"))
    }
}
