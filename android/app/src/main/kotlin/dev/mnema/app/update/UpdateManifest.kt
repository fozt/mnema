package dev.mnema.app.update

import dev.mnema.app.i18n.tr
import java.io.File
import java.io.IOException
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

internal val BAD_MANIFEST: String get() = tr("Couldn't check for updates. Try again later")

private const val MAX_APK_BYTES = 512L * 1024 * 1024

// like _VERSION in desktop update.py: the version goes into the file name
private val VERSION = Regex("(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)")
private val SHA256 = Regex("[a-f0-9]{64}")

private val json = Json { ignoreUnknownKeys = true }

/** Description of a released APK: `latest/mnema-android.json` in the package registry. */
@Serializable
data class UpdateManifest(
    val version: String,
    @SerialName("version_code") val versionCode: Long,
    val apk: String,
    val sha256: String,
    val size: Long,
    @SerialName("min_sdk") val minSdk: Int,
)

class UpdateException(message: String) : Exception(message)

fun parseManifest(text: String): UpdateManifest {
    val manifest = try {
        json.decodeFromString<UpdateManifest>(text)
    } catch (_: SerializationException) {
        throw UpdateException(BAD_MANIFEST)
    } catch (_: IllegalArgumentException) {
        throw UpdateException(BAD_MANIFEST)
    }
    if (!valid(manifest)) throw UpdateException(BAD_MANIFEST)
    return manifest
}

private fun valid(manifest: UpdateManifest): Boolean {
    val url = manifest.apk.toHttpUrlOrNull()
    return VERSION.matches(manifest.version) &&
        manifest.versionCode > 0 &&
        url != null && url.isHttps && url.username.isEmpty() && url.password.isEmpty() &&
        SHA256.matches(manifest.sha256) &&
        manifest.size in 1..MAX_APK_BYTES &&
        manifest.minSdk > 0
}

/** The manifest of the last check: the found version stays visible after a restart. */
fun saveManifest(file: File, manifest: UpdateManifest) {
    val part = File(file.path + ".part")
    part.writeText(json.encodeToString(manifest))
    if (!part.renameTo(file)) {
        part.delete()
        throw IOException(tr("Manifest not saved: {file}", "file" to file))
    }
}

fun loadManifest(file: File): UpdateManifest? = try {
    parseManifest(file.readText())
} catch (_: IOException) {
    null
} catch (_: UpdateException) {
    null
}

/** What to do with a release: the same or older — nothing, newer Android than the phone has — not compatible. */
fun decide(installedCode: Long, manifest: UpdateManifest, sdk: Int): UpdateStage = when {
    manifest.versionCode <= installedCode -> UpdateStage.UpToDate
    manifest.minSdk > sdk -> UpdateStage.Unsuitable(manifest)
    else -> UpdateStage.Available(manifest)
}
