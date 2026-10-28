package dev.mnema.app.sync

import dev.mnema.app.i18n.tr
import dev.mnema.app.server.Connection
import dev.mnema.app.server.RunnerClient
import dev.mnema.app.server.parseConnection
import java.io.IOException
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.DeserializationStrategy
import kotlinx.serialization.json.jsonObject
import okhttp3.Call
import okhttp3.Callback
import okhttp3.Headers
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/** One selected server; an ambiguous reply is checked by the next sync, not by retrying an HTTP write. */
class LibraryClient(connection: Connection, http: OkHttpClient? = null) : AutoCloseable {
    private val connection = connection.copy(url = parseConnection(connection.url).url)
    val server: String get() = connection.url
    private val owned = http == null
    private val http = (http ?: RunnerClient.defaultHttp()).newBuilder()
        .retryOnConnectionFailure(false).followRedirects(false).followSslRedirects(false).build()

    init { if (connection.invite != null) throw SyncException(tr("Connect this device to the server first")) }

    suspend fun manifest(): Manifest {
        val reply = send(request().get().build())
        val fields = try { wire.parseToJsonElement(utf8(reply.bytes)).jsonObject } catch (_: IllegalArgumentException) { throw badReply() }
        if ("records" !in fields || "sections" !in fields) throw badReply()
        return decode(reply, Manifest.serializer())
    }

    suspend fun create(id: String, path: String, files: Map<FileName, FileBody>): Record =
        decode(send(request("records").post(wire.encodeToString(CreateRecord.serializer(), CreateRecord(id, path, files)).toRequestBody(JSON)).build()), Record.serializer())

    suspend fun read(id: String, name: FileName): FileData {
        val reply = send(request("records", id, "files", name.fileName).get().build())
        val stamp = Stamp(
            reply.headers["ETag"]?.removeSurrounding("\"") ?: throw badReply(),
            reply.headers["X-Mnema-Mtime-Ns"]?.toLongOrNull() ?: throw badReply(),
        )
        if (sha256(reply.bytes) != stamp.sha256) throw SyncException(tr("The received file does not match its checksum"))
        utf8(reply.bytes)
        return FileData(reply.bytes, stamp)
    }

    suspend fun write(id: String, name: FileName, bytes: ByteArray, mtimeNs: Long, expected: String?): Stamp {
        utf8(bytes)
        val request = request("records", id, "files", name.fileName).header("X-Mnema-Mtime-Ns", mtimeNs.toString())
        if (expected == null) request.header("If-None-Match", "*") else request.header("If-Match", expected)
        return decode(send(request.put(bytes.toRequestBody(TEXT)).build()), Stamp.serializer())
    }

    suspend fun removeFile(id: String, name: FileName, expected: String) {
        send(request("records", id, "files", name.fileName).header("If-Match", expected).delete().build())
    }

    suspend fun move(id: String, path: String, expectedPath: String): Record = decode(
        send(request("records", id).patch(wire.encodeToString(MoveRecord.serializer(), MoveRecord(expectedPath, path)).toRequestBody(JSON)).build()), Record.serializer(),
    )

    suspend fun remove(id: String, expectedTag: String) {
        send(request("records", id).header("If-Match", expectedTag).delete().build())
    }

    suspend fun section(path: String, delete: Boolean = false) {
        val request = request("sections")
        request.url(request.build().url.newBuilder().addQueryParameter("path", path).build())
        send(if (delete) request.delete().build() else request.put(ByteArray(0).toRequestBody()).build())
    }

    private fun request(vararg segments: String): Request.Builder {
        val url = server.toHttpUrl().newBuilder().addPathSegment("api").addPathSegment("library")
        segments.forEach(url::addPathSegment)
        return Request.Builder().url(url.build()).apply { connection.token?.let { header("Authorization", "Bearer $it") } }
    }

    private data class Reply(val code: Int, val headers: Headers, val bytes: ByteArray)
    private suspend fun send(request: Request): Reply {
        val reply = suspendCancellableCoroutine { continuation ->
            val call = http.newCall(request)
            continuation.invokeOnCancellation { call.cancel() }
            call.enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) { continuation.resumeWithException(unreachable()) }
                override fun onResponse(call: Call, response: Response) {
                    val result = try { response.use { Reply(it.code, it.headers, it.body.bytes()) } }
                    catch (_: IOException) { continuation.resumeWithException(unreachable()); return }
                    continuation.resume(result)
                }
            })
        }
        when (reply.code) {
            in 200..299 -> return reply
            409 -> throw SyncException(tr("Files on the server changed; sync will run again"))
            404 -> throw SyncException(tr("The library or recording on the selected server is unavailable"))
            401, 403 -> throw SyncException(tr("A new invitation from the server is needed"))
            else -> throw SyncException(tr("Could not read or change the server library"))
        }
    }

    private fun <T> decode(reply: Reply, serializer: DeserializationStrategy<T>): T = try {
        wire.decodeFromString(serializer, utf8(reply.bytes))
    } catch (_: IllegalArgumentException) { throw badReply() }

    override fun close() {
        if (owned) {
            http.dispatcher.executorService.shutdown()
            http.connectionPool.evictAll()
        }
    }

    private fun badReply() = SyncException(tr("The server returned incomplete library data"))
    private fun unreachable() = SyncException(tr("The sync server is unavailable; files are kept"))
    private companion object {
        val JSON = "application/json; charset=utf-8".toMediaType()
        val TEXT = "text/plain; charset=utf-8".toMediaType()
    }
}
