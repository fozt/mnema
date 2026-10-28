package dev.mnema.app.sync

import dev.mnema.app.server.Connection
import dev.mnema.app.server.RunnerClient
import java.io.IOException
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.SerializationException
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test

class LibraryClientTest {
    private val server = MockWebServer()
    private val http = RunnerClient.defaultHttp()
    @Before fun start() { server.start() }
    @After fun stop() { server.close(); http.dispatcher.executorService.shutdown(); http.connectionPool.evictAll() }
    private fun client() = LibraryClient(Connection(server.url("/prefix").toString(), "device-token"), http)

    @Test fun httpContractUsesExactPreconditionsRawUtf8AndLongNanoseconds() = runBlocking<Unit> {
        val create = wire.decodeFromJsonElement(CreateRecord.serializer(), fixture().getValue("create"))
        val record = wire.decodeFromJsonElement(Manifest.serializer(), fixture().getValue("manifest")).records.single()
        val client = client()
        server.enqueue(json(wire.encodeToString(Record.serializer(), record)))
        assertEquals(record, client.create(create.id, create.path, create.files))
        val post = server.takeRequest()
        assertEquals("/prefix/api/library/records", post.target)
        assertEquals("Bearer device-token", post.headers["Authorization"])
        assertFalse("device-token" in post.target)
        assertEquals(create, wire.decodeFromString(CreateRecord.serializer(), post.body!!.utf8()))
        val body = create.files.getValue(FileName.TRANSCRIPT)
        val stamp = record.files.getValue(FileName.TRANSCRIPT)
        server.enqueue(MockResponse.Builder().body(body.text).addHeader("ETag", "\"${stamp.sha256}\"").addHeader("X-Mnema-Mtime-Ns", body.mtimeNs.toString()).build())
        val read = client.read(create.id, FileName.TRANSCRIPT)
        assertArrayEquals(body.text.toByteArray(), read.bytes); assertEquals(stamp, read.stamp)
        server.takeRequest()
        for (expected in listOf(null, "previous-sha")) {
            server.enqueue(json(wire.encodeToString(Stamp.serializer(), stamp)))
            assertEquals(stamp, client.write(create.id, FileName.TRANSCRIPT, read.bytes, body.mtimeNs, expected))
            val put = server.takeRequest()
            assertEquals("PUT", put.method)
            assertEquals(expected, put.headers["If-Match"])
            assertEquals(if (expected == null) "*" else null, put.headers["If-None-Match"])
            assertEquals(body.mtimeNs.toString(), put.headers["X-Mnema-Mtime-Ns"])
            assertEquals(body.text, put.body!!.utf8())
        }
        server.enqueue(response(204)); client.removeFile(create.id, FileName.SUMMARY, "file-sha")
        assertEquals("file-sha", server.takeRequest().headers["If-Match"])
        server.enqueue(response(204)); client.remove(create.id, "opaque-tag")
        assertEquals("opaque-tag", server.takeRequest().headers["If-Match"])
        server.enqueue(json(wire.encodeToString(Record.serializer(), record))); client.move(create.id, "Работа/Новое", "Работа/Старое")
        val patch = server.takeRequest()
        assertEquals(MoveRecord("Работа/Старое", "Работа/Новое"), wire.decodeFromString(MoveRecord.serializer(), patch.body!!.utf8()))
        server.enqueue(response(204)); client.section("Работа & Учёба", true)
        val section = server.takeRequest()
        assertEquals("DELETE", section.method)
        assertEquals("Работа & Учёба", section.url.queryParameter("path"))
    }

    @Test fun invalidHashUtf8MissingManifestOrImpreciseTimesAreNotAccepted() {
        server.enqueue(MockResponse.Builder().body("different").addHeader("ETag", "wrong").addHeader("X-Mnema-Mtime-Ns", "100").build())
        assertThrows(SyncException::class.java) { runBlocking { client().read("id", FileName.TRANSCRIPT) } }
        server.enqueue(json("{}"))
        assertThrows(SyncException::class.java) { runBlocking { client().manifest() } }
        for (value in listOf("\"1792731600123456789\"", "1.5", "1e3", "9223372036854775808", "true", "null")) {
            assertThrows("mtime_ns=$value", SerializationException::class.java) { wire.decodeFromString(Stamp.serializer(), """{"sha256":"s","mtime_ns":$value}""") }
        }
        for (ns in listOf(Long.MIN_VALUE, Long.MAX_VALUE, 1792731600123456789L)) {
            val stamp = Stamp("s", ns)
            assertEquals(stamp, wire.decodeFromString(Stamp.serializer(), wire.encodeToString(Stamp.serializer(), stamp)))
        }
    }

    @Test fun refusalAndRedirectDoNotLeakBodySecretsOrFollowAnotherServer() {
        val other = MockWebServer(); other.start()
        try {
            for (code in listOf(401, 403, 404, 409, 503, 302)) {
                server.enqueue(MockResponse.Builder().code(code).body("device-token private file contents").addHeader("Location", other.url("/")).build())
                val error = assertThrows(IOException::class.java) { runBlocking { client().manifest() } }
                assertFalse("device-token" in error.message.orEmpty())
                assertFalse("private file" in error.message.orEmpty())
            }
            assertEquals(0, other.requestCount)
            assertEquals(6, server.requestCount)
        } finally { other.close() }
    }
}
