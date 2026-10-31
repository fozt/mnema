package dev.mnema.app.server

import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import dev.mnema.app.i18n.AcceptLanguage
import dev.mnema.app.i18n.tr
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okio.BufferedSink

private val UNREACHABLE_TEXT: String get() = tr("The server is not responding. Check the address and make sure the server is running")
private val TOKEN_REJECTED_TEXT: String get() = tr("A new invitation from the server is needed")
private val NOT_MNEMA_TEXT: String get() = tr("There is no Mnema server at this address")
private val QUEUE_FULL_TEXT: String get() = tr("The server is busy: the queue is full. Try again later")
private val UPLOAD_GONE_TEXT: String get() = tr("The file is no longer available on the server. Upload the recording again")
private val BAD_RESPONSE_TEXT: String get() = tr("The server gave an unexpected response")
private val INVITE_REFUSED_TEXT: String get() = tr("The server refused the invitation")
// jobs.KEY_TAKEN
private val KEY_TAKEN_TEXT: String get() = tr("The retry key is taken by another job")
// 409 on POST /api/jobs: the retry key belongs to a job this device cannot see (jobs.KEY_REASON)
private const val KEY_TAKEN_REASON = "key_taken"

private const val MAX_REPLY_BYTES = 16L * 1024 * 1024

/** Result of an address check, `GET /api/capabilities`, like `bar/forms.py::ServerFlow`. */
sealed interface Health {
    data object Ok : Health
    data object Unreachable : Health
    data object TokenRequired : Health
    data object TokenRejected : Health
    data object NotMnema : Health
}

fun Health.message(): String? = when (this) {
    Health.Ok -> null
    Health.Unreachable -> UNREACHABLE_TEXT
    Health.TokenRequired -> PASTE_INVITE
    Health.TokenRejected -> TOKEN_REJECTED_TEXT
    Health.NotMnema -> NOT_MNEMA_TEXT
}

enum class RunnerErrorKind { UNREACHABLE, TOKEN, QUEUE_FULL, UPLOAD_GONE, KEY_TAKEN, JOB_FAILED, BAD_RESPONSE }

class RunnerException(
    val kind: RunnerErrorKind,
    message: String,
    val track: String? = null,
    val requestSent: Boolean = false,
) : IOException(message)

/** Only for submitting a shared job: the key is saved and the body sent, but the reply may never come. */
private class SubmissionBody(private val body: RequestBody) : RequestBody() {
    @Volatile var sent = false
        private set
    override fun contentType() = body.contentType()
    override fun contentLength() = body.contentLength()
    override fun writeTo(sink: BufferedSink) {
        body.writeTo(sink)
        sink.flush()
        sent = true
    }
}

/** An upload on the node: [offset] — bytes the node accepted, [path] — the path for the job once everything arrived. */
@Serializable
data class UploadInfo(val offset: Long, val path: String? = null, val name: String? = null, val size: Long? = null)

/** What the node can do (`GET /api/capabilities`); when [health] is not Ok, the rest is unknown; [version] — its Mnema version, null before 0.3.2. */
data class NodeCheck(val health: Health, val llm: Boolean = false, val name: String? = null, val version: String? = null)

/** Node details; the model and its settings come only from the server configuration. */
@Serializable
data class NodeSettings(val name: String, val model: String? = null, val engines: Boolean = false)

private val json = Json { ignoreUnknownKeys = true }
private val recordingJson = Json { encodeDefaults = true }
private val JSON_TYPE = "application/json; charset=utf-8".toMediaType()
private val BINARY_TYPE = "application/octet-stream".toMediaType()

private fun unreachable(requestSent: Boolean = false) = RunnerException(RunnerErrorKind.UNREACHABLE, UNREACHABLE_TEXT, requestSent = requestSent)

private fun gone() = RunnerException(RunnerErrorKind.UPLOAD_GONE, UPLOAD_GONE_TEXT)

private fun uploadInfo(body: String): UploadInfo = json.decodeFromString(UploadInfo.serializer(), body)

private fun conflictOffset(reply: Reply): UploadInfo? = try {
    uploadInfo(reply.body)
} catch (_: IllegalArgumentException) {
    null
}

private fun String.jsonString(key: String): String? =
    json.parseToJsonElement(this).jsonObject[key]?.jsonPrimitive?.takeIf { it.isString }?.content

private class Reply(val code: Int, val body: String) {
    val ok: Boolean get() = code in 200..299
}

/** [named] — the node named itself in a successful capabilities reply; the name in the node list follows it; [versioned] — its version in that reply. */
class RunnerClient(
    val connection: Connection,
    private val http: OkHttpClient = defaultHttp(),
    private val versioned: (String?) -> Unit = {},
    private val named: (String) -> Unit = {},
) {
    suspend fun submitRecording(key: String, tracks: Map<String, String>, params: RecordingParams): String =
        submitNodeRecording(buildJsonObject {
            put("op", "recording")
            put("key", key)
            put("tracks", JsonObject(tracks.mapValues { JsonPrimitive(it.value) }))
            put("params", recordingJson.encodeToJsonElement(RecordingParams.serializer(), params))
        })

    suspend fun submitRecordingSummary(key: String, path: String, params: SummaryParams): String =
        submitNodeRecording(buildJsonObject {
            put("op", "recording_summary")
            put("key", key)
            put("path", path)
            put("params", recordingJson.encodeToJsonElement(SummaryParams.serializer(), params))
        })

    private suspend fun submitNodeRecording(payload: JsonObject): String {
        val body = SubmissionBody(json.encodeToString(JsonObject.serializer(), payload).toRequestBody(JSON_TYPE))
        val reply = send(request("api", "jobs").post(body).build(), http.newBuilder().retryOnConnectionFailure(false).build())
        if (reply.code == 404 || reply.code == 409) {
            val (track, reason) = try {
                reply.body.jsonString("track") to reply.body.jsonString("reason")
            } catch (_: IllegalArgumentException) { null to null }
            if (reply.code == 409 && reason == KEY_TAKEN_REASON) {
                throw RunnerException(RunnerErrorKind.KEY_TAKEN, detail(reply) ?: KEY_TAKEN_TEXT)
            }
            // the upload is missing or held by another job: upload the track again
            throw RunnerException(RunnerErrorKind.UPLOAD_GONE, detail(reply) ?: UPLOAD_GONE_TEXT, track)
        }
        if (reply.code == 400 || reply.code == 422) {
            throw RunnerException(RunnerErrorKind.JOB_FAILED, detail(reply) ?: BAD_RESPONSE_TEXT)
        }
        if (!reply.ok) throw failure(reply)
        return decode(reply) { it.jsonString("id")?.takeIf(String::isNotBlank) }
    }

    /** A node job; null — the node has no such job for this device. */
    suspend fun nodeJob(id: String): NodeJob? {
        val reply = send(request("api", "jobs", id).get().build())
        if (reply.code == 404) return null
        if (!reply.ok) throw failure(reply)
        return decode(reply) { json.decodeFromString(NodeJob.serializer(), it) }
    }

    suspend fun forget(id: String) {
        val reply = send(request("api", "jobs", id).delete().build())
        if (!reply.ok && reply.code != 404) throw failure(reply)
    }

    suspend fun asrChoices(): List<AsrOption> {
        val reply = send(request("api", "node", "asr").get().build())
        if (!reply.ok) throw failure(reply)
        return decode(reply) { json.decodeFromString(AsrChoices.serializer(), it).choices }
    }

    /** Starts a chunked upload; a missing API is an ordinary node error. */
    suspend fun startUpload(name: String, size: Long): String {
        val payload = buildJsonObject {
            put("name", name)
            put("size", size)
        }
        val body = json.encodeToString(JsonObject.serializer(), payload).toRequestBody(JSON_TYPE)
        val reply = send(request("api", "uploads").post(body).build())
        if (!reply.ok) throw failure(reply)
        return decode(reply) { it.jsonString("id") }
    }

    /** How much the node accepted; null — the upload is gone from the node, it expired. */
    suspend fun uploadOffset(id: String): UploadInfo? {
        val reply = send(request("api", "uploads", id).get().build())
        if (reply.code == 404) return null
        if (!reply.ok) throw failure(reply)
        return decode(reply, ::uploadInfo)
    }

    /** A chunk of [count] bytes at [offset]; on 409 and 408 — the node's offset to continue from. */
    suspend fun uploadChunk(id: String, offset: Long, bytes: ByteArray, count: Int): UploadInfo {
        val url = url("api", "uploads", id).newBuilder().addQueryParameter("offset", offset.toString()).build()
        val reply = send(request(url).put(bytes.toRequestBody(BINARY_TYPE, 0, count)).build())
        if (reply.code == 409 || reply.code == 408) {
            // no offset in the reply, so ask the node separately
            return conflictOffset(reply) ?: uploadOffset(id) ?: throw gone()
        }
        if (!reply.ok) throw failure(reply, gone = setOf(404))
        return decode(reply, ::uploadInfo)
    }

    /** Cancelling is best effort: no error for a job the runner finished or forgot. */
    suspend fun cancel(id: String) {
        val reply = send(request("api", "jobs", id, "cancel").post(ByteArray(0).toRequestBody()).build())
        if (!reply.ok && reply.code != 404) throw failure(reply)
    }

    /** Whether the node answers within [timeoutMs] and what it can do; anything but a clear server reply counts as not responding. */
    suspend fun capabilities(timeoutMs: Long = 3000): NodeCheck {
        val client = http.newBuilder()
            .connectTimeout(timeoutMs, TimeUnit.MILLISECONDS)
            .readTimeout(timeoutMs, TimeUnit.MILLISECONDS)
            .callTimeout(timeoutMs, TimeUnit.MILLISECONDS)
            .build()
        val reply = try {
            send(request("api", "capabilities").get().build(), client)
        } catch (_: RunnerException) {
            return NodeCheck(Health.Unreachable)
        }
        val problem = when {
            reply.code == 401 || reply.code == 403 ->
                if (connection.token == null) Health.TokenRequired else Health.TokenRejected
            reply.code == 404 -> Health.NotMnema
            !reply.ok -> Health.Unreachable
            !isCapabilities(reply.body) -> Health.NotMnema
            else -> null
        }
        if (problem != null) return NodeCheck(problem)
        val body = json.parseToJsonElement(reply.body).jsonObject
        val name = (body["name"] as? JsonPrimitive)?.takeIf { it.isString }?.content?.trim()?.ifEmpty { null }
        name?.let(named)
        val version = (body["version"] as? JsonPrimitive)?.takeIf { it.isString }?.content
        versioned(version)
        return NodeCheck(Health.Ok, llm = body["llm"].strictBoolean() == true, name = name, version = version)
    }

    /** Exchanges a node invitation for this device's token; the request has no token, a refusal carries the node's text without the code. */
    suspend fun claimInvite(code: String, name: String): String {
        val payload = buildJsonObject {
            put("invite", code)
            put("name", name)
            put("library", true) // this app knows library numbers: a server user's invitation needs it
        }
        val body = json.encodeToString(JsonObject.serializer(), payload).toRequestBody(JSON_TYPE)
        val reply = send(Request.Builder().url(url("api", "devices", "claim")).post(body).build())
        if (reply.code == 403) throw RunnerException(RunnerErrorKind.TOKEN, detail(reply) ?: INVITE_REFUSED_TEXT)
        if (!reply.ok) throw failure(reply)
        return decode(reply) { it.jsonString("token")?.ifEmpty { null } }
    }

    suspend fun nodeSettings(): NodeSettings {
        val reply = send(request("api", "node", "settings").get().build())
        if (!reply.ok) throw failure(reply)
        return decode(reply) { json.decodeFromString(NodeSettings.serializer(), it) }
    }

    private fun detail(reply: Reply): String? = try {
        reply.body.jsonString("detail")?.ifEmpty { null }
    } catch (_: IllegalArgumentException) {
        null
    }

    private fun request(vararg segments: String): Request.Builder = request(url(*segments))

    private fun request(url: HttpUrl): Request.Builder {
        val builder = Request.Builder().url(url)
        connection.token?.let { builder.header("Authorization", "Bearer $it") }
        return builder
    }

    private fun url(vararg segments: String): HttpUrl {
        var url: HttpUrl = connection.url.toHttpUrlOrNull() ?: throw unreachable()
        for (segment in segments) url = url.newBuilder().addPathSegment(segment).build()
        return url
    }

    private fun failure(reply: Reply, gone: Set<Int> = emptySet()): RunnerException = when {
        reply.code == 401 || reply.code == 403 -> {
            val text = if (connection.token == null) PASTE_INVITE else TOKEN_REJECTED_TEXT
            RunnerException(RunnerErrorKind.TOKEN, text)
        }
        reply.code == 429 -> RunnerException(RunnerErrorKind.QUEUE_FULL, QUEUE_FULL_TEXT)
        reply.code in gone -> gone()
        // a gateway answers like this while the runner restarts
        reply.code in 502..504 -> unreachable()
        else -> RunnerException(RunnerErrorKind.BAD_RESPONSE, tr("The server returned error {code}", "code" to reply.code))
    }

    private inline fun <T : Any> decode(reply: Reply, parse: (String) -> T?): T {
        val value = try {
            parse(reply.body)
        } catch (_: IllegalArgumentException) {
            null
        }
        return value ?: throw RunnerException(RunnerErrorKind.BAD_RESPONSE, BAD_RESPONSE_TEXT)
    }

    // A dropped connection, also mid-body, is UNREACHABLE; cancelling the coroutine cancels the call.
    private suspend fun send(request: Request, client: OkHttpClient = http): Reply =
        suspendCancellableCoroutine { continuation ->
            val call = client.newCall(request)
            continuation.invokeOnCancellation { call.cancel() }
            call.enqueue(
                object : Callback {
                    override fun onFailure(call: Call, e: IOException) {
                        continuation.resumeWithException(unreachable((request.body as? SubmissionBody)?.sent == true))
                    }

                    override fun onResponse(call: Call, response: Response) {
                        val reply = try {
                            response.use { Reply(it.code, it.peekBody(MAX_REPLY_BYTES).string()) }
                        } catch (_: IOException) {
                            continuation.resumeWithException(unreachable(response.isSuccessful && (request.body as? SubmissionBody)?.sent == true))
                            return
                        }
                        continuation.resume(reply)
                    }
                },
            )
        }

    companion object {
        /** No redirects, so the token never leaves the address from the connection string. */
        fun defaultHttp(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .followRedirects(false)
            .followSslRedirects(false)
            .addInterceptor(AcceptLanguage)
            .build()
    }
}

private fun isCapabilities(body: String): Boolean {
    val capabilities = try {
        json.parseToJsonElement(body) as? JsonObject
    } catch (_: IllegalArgumentException) {
        null
    } ?: return false
    return capabilities["ok"].strictBoolean() == true && capabilities["llm"].strictBoolean() != null
}

private fun JsonElement?.strictBoolean(): Boolean? = (this as? JsonPrimitive)?.takeIf { !it.isString }?.booleanOrNull
