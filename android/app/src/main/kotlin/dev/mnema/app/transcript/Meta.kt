package dev.mnema.app.transcript

import dev.mnema.app.i18n.tr
import dev.mnema.app.recordings.startedIso
import dev.mnema.app.sync.filesLock
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.time.Instant
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale
import java.util.TimeZone
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.doubleOrNull

/** `.mnema-meta.json`: a recording's processing history, in the `live/meta.py` format. */
object Meta {
    const val FILE = ".mnema-meta.json"
    const val VERSION = 1

    @OptIn(ExperimentalSerializationApi::class)
    private val pretty = Json {
        prettyPrint = true
        prettyPrintIndent = "  "
    }
    private val isoSeconds = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ssxxx", Locale.ROOT)

    /** Applying a result requires the history ([strict]); stopping processing writes it when possible. */
    fun appendRun(dir: File, run: JsonObject, strict: Boolean = false) {
        update(dir, strict) { data ->
            val runs = runsOf(data).orEmpty()
            val job = string(run["job"])
            if (job != null && runs.any { string((it as? JsonObject)?.get("job")) == job }) data
            else JsonObject(data + ("runs" to JsonArray(runs + run)))
        }
    }

    fun read(dir: File): JsonObject? = parse(File(dir, FILE)) as? JsonObject

    /** The recording identity and start, if missing; [started] is computed only then, null — the start is unknown and not written. */
    fun ensureRecording(dir: File, id: String, started: () -> String?) {
        update(dir) { data ->
            val next = data.toMutableMap()
            if (string(next["id"]) == null) next["id"] = JsonPrimitive(id)
            if (string(next["started"]) == null) started()?.let { next["started"] = JsonPrimitive(it) }
            JsonObject(next)
        }
    }

    fun started(dir: File): String? = string(read(dir)?.get("started"))

    fun lastRun(dir: File): JsonObject? = lastRun(read(dir))

    /** The last run except a stopped one (nothing to say about it): remarks are about it, and a summary run takes it as the previous one (shown-run-v1). */
    fun lastRun(data: JsonObject?): JsonObject? = (data?.get("runs") as? JsonArray).orEmpty().mapNotNull { it as? JsonObject }
        .lastOrNull { string(it["status"]) != "stopped" }

    fun hasJob(dir: File, job: String): Boolean =
        (read(dir)?.get("runs") as? JsonArray).orEmpty().any { string((it as? JsonObject)?.get("job")) == job }

    /** A part has its own identity but the source recording's start. */
    fun createPart(source: File, part: File, id: String) = synchronized(filesLock) {
        val data = mapOf<String, JsonElement>(
            "version" to JsonPrimitive(VERSION), "id" to JsonPrimitive(id), "runs" to JsonArray(emptyList()),
        ) + listOfNotNull((started(source) ?: startedIso(source))?.let { "started" to JsonPrimitive(it) })
        write(part, JsonObject(data))
    }

    private fun update(dir: File, strict: Boolean = true, change: (JsonObject) -> JsonObject) = synchronized(filesLock) {
        try {
            val file = File(dir, FILE)
            if (Files.isSymbolicLink(file.toPath()) || file.isDirectory) throw IOException(tr("The recording history is unavailable"))
            var data = if (file.exists()) {
                try { Json.parseToJsonElement(file.readText(Charsets.UTF_8)) as? JsonObject } catch (_: SerializationException) { null }
            } else null
            if (data != null && !isOurVersion(data["version"])) throw IOException(tr("The recording history format is newer than the app"))
            if (file.exists() && (data == null || runsOf(data) == null)) {
                Files.move(file.toPath(), File(dir, "$FILE.bad").toPath(), StandardCopyOption.REPLACE_EXISTING)
                data = data?.let { JsonObject(it - "runs") }
            }
            val before = JsonObject(data.orEmpty() + mapOf("version" to JsonPrimitive(VERSION), "runs" to JsonArray(runsOf(data).orEmpty())))
            val after = change(before)
            if (data == null || after != data) write(dir, after)
        } catch (e: IOException) {
            if (strict) throw e
        }
    }

    private fun write(dir: File, data: JsonObject) {
        val text = pretty.encodeToString(JsonElement.serializer(), data) + "\n"
        atomicWrite(File(dir, FILE), text.toByteArray(Charsets.UTF_8))
    }

    private fun string(value: JsonElement?): String? = (value as? JsonPrimitive)?.takeIf { it.isString }?.content

    /** ISO-8601 with an offset and whole seconds: `2026-10-21T14:35:00+03:00`. */
    fun isoNow(epochMs: Long, tz: TimeZone): String = iso(Instant.ofEpochMilli(epochMs).atZone(tz.toZoneId()))

    fun iso(at: ZonedDateTime): String = isoSeconds.format(at)

    private fun runsOf(data: JsonElement?): List<JsonElement>? {
        if (data !is JsonObject) return null
        return if ("runs" in data) (data["runs"] as? JsonArray)?.toList() else emptyList()
    }

    private fun parse(file: File): JsonElement? = try {
        if (Files.isSymbolicLink(file.toPath())) null else Json.parseToJsonElement(file.readText(Charsets.UTF_8))
    } catch (_: IOException) {
        null
    } catch (_: SerializationException) {
        null
    }

    private fun isOurVersion(version: JsonElement?): Boolean =
        version == null || version is JsonNull ||
            (version is JsonPrimitive && !version.isString && version.doubleOrNull == VERSION.toDouble())

}
