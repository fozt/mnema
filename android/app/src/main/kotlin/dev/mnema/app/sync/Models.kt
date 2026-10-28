package dev.mnema.app.sync

import java.io.IOException
import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerializationException
import kotlinx.serialization.descriptors.PrimitiveKind
import kotlinx.serialization.descriptors.PrimitiveSerialDescriptor
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.JsonDecoder
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

internal val wire = Json { ignoreUnknownKeys = true; encodeDefaults = true }

@Serializable
enum class FileName(val fileName: String) {
    @SerialName("transcript.txt") TRANSCRIPT("transcript.txt"),
    @SerialName("summary.md") SUMMARY("summary.md"),
    @SerialName(".mnema-meta.json") META(".mnema-meta.json"),
}

@Serializable
data class Stamp(val sha256: String, @SerialName("mtime_ns") @Serializable(with = NanosecondsSerializer::class) val mtimeNs: Long)

@Serializable
data class Record(val id: String, val path: String, val files: Map<FileName, Stamp>, val tag: String) {
    init { recordingId(id) }
}

@Serializable
data class Manifest(val records: List<Record> = emptyList(), val sections: List<String> = emptyList())

@Serializable
data class FileBody(val text: String, @SerialName("mtime_ns") @Serializable(with = NanosecondsSerializer::class) val mtimeNs: Long)

@Serializable
internal data class CreateRecord(val id: String, val path: String, val files: Map<FileName, FileBody>)

@Serializable
internal data class MoveRecord(@SerialName("expected_path") val expectedPath: String, val path: String)

@Serializable
data class Checkpoint(val server: String = "", val records: Map<String, Record> = emptyMap(), val sections: List<String> = emptyList())

data class Scan(val manifest: Manifest = Manifest(), val complete: Boolean = true, val blockedPaths: List<String> = emptyList(), val missingRoot: Boolean = false,
    val nested: List<String> = emptyList(), // recordings inside a recording; the path is shown to the user
    val deep: List<String> = emptyList(), // recordings in a subfolder of a section
    val withoutFiles: List<String> = emptyList()) // known recordings without their files

// bulkMissing — gone on this device, serverMissing — gone on the server; both wait for the user's choice.
// withoutTrash — there is no trash: what vanished on the server is deleted here only forever
data class SyncStatus(val bulkMissing: MutableList<String> = mutableListOf(), val errors: MutableList<String> = mutableListOf(), var changed: Boolean = false,
    val serverMissing: MutableList<String> = mutableListOf(), var withoutTrash: Boolean = false)

class SyncException(message: String) : IOException(message)

internal fun recordingId(value: String): String {
    // accepts the same UUID text forms as Python, keeping the original string
    val hex = value.removePrefix("urn:uuid:").removeSurrounding("{", "}").replace("-", "")
    require(hex.length == 32 && hex.all { it in "0123456789abcdefABCDEF" }) { "Invalid recording id" }
    return value
}

/** A JSON integer exactly like Pydantic MtimeNs: no strings, floats or Double precision loss. */
internal object NanosecondsSerializer : KSerializer<Long> {
    override val descriptor = PrimitiveSerialDescriptor("MtimeNs", PrimitiveKind.LONG)
    override fun serialize(encoder: Encoder, value: Long) = encoder.encodeLong(value)
    override fun deserialize(decoder: Decoder): Long {
        if (decoder !is JsonDecoder) return decoder.decodeLong()
        val value = decoder.decodeJsonElement() as? JsonPrimitive
        val literal = value?.takeUnless { it.isString }?.content
        return literal?.takeIf { Regex("-?(0|[1-9][0-9]*)").matches(it) }?.toLongOrNull()
            ?: throw SerializationException("mtime_ns must be an integer Long")
    }
}
