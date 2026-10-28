package dev.mnema.app.update

import dev.mnema.app.i18n.tr
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStream
import java.security.MessageDigest
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request

internal val CORRUPT: String get() = tr("The update file is damaged")
internal val DOWNLOAD_FAILED: String get() = tr("Couldn't download the update. Try again later")
private val NOT_SAVED: String get() = tr("Couldn't save the update")

private const val CHUNK = 64 * 1024

/** The release APK in `cacheDir/updates/mnema-<version>.apk`; accepted only with the size and SHA-256 from the manifest. */
class ApkDownloader(private val http: OkHttpClient, private val dir: File) {
    suspend fun download(manifest: UpdateManifest, onProgress: (Float) -> Unit): File = withContext(Dispatchers.IO) {
        val target = File(dir, "mnema-${manifest.version}.apk")
        val part = File(dir, "${target.name}.part")
        dir.listFiles()?.filter { it != target }?.forEach { it.delete() }
        if (matches(target, manifest.size, manifest.sha256)) {
            onProgress(1f)
            return@withContext target
        }
        target.delete()
        if (!dir.isDirectory && !dir.mkdirs()) throw UpdateException(NOT_SAVED)
        try {
            fetch(manifest, part, onProgress)
            if (!matches(part, manifest.size, manifest.sha256)) throw UpdateException(CORRUPT)
            if (!part.renameTo(target)) throw UpdateException(NOT_SAVED)
        } finally {
            part.delete()
        }
        target
    }

    fun clear() {
        dir.listFiles()?.forEach { it.delete() }
    }

    private suspend fun fetch(manifest: UpdateManifest, part: File, onProgress: (Float) -> Unit) {
        val request = Request.Builder().url(manifest.apk).build()
        try {
            http.newCall(request).execute().use { response ->
                if (response.code in setOf(401, 403, 404)) throw UpdateException(NOT_PUBLISHED)
                if (!response.isSuccessful) throw UpdateException(DOWNLOAD_FAILED)
                response.body.byteStream().use { input -> save(input, part, manifest.size, onProgress) }
            }
        } catch (_: IOException) {
            throw UpdateException(NO_NETWORK)
        }
    }

    // reading is the network, writing is the disk: failures get different words
    private suspend fun save(input: InputStream, part: File, size: Long, onProgress: (Float) -> Unit) {
        val out = try {
            FileOutputStream(part)
        } catch (_: IOException) {
            throw UpdateException(NOT_SAVED)
        }
        out.use {
            val buffer = ByteArray(CHUNK)
            var written = 0L
            var shown = -1
            while (true) {
                currentCoroutineContext().ensureActive()
                val read = input.read(buffer)
                if (read < 0) break
                written += read
                if (written > size) throw UpdateException(CORRUPT)
                try {
                    out.write(buffer, 0, read)
                } catch (_: IOException) {
                    throw UpdateException(NOT_SAVED)
                }
                val percent = (written * 100 / size).toInt()
                if (percent != shown) {
                    shown = percent
                    onProgress(written.toFloat() / size)
                }
            }
        }
    }
}

/** A file of the size and SHA-256 given in the manifest. */
fun matches(file: File, size: Long, sha256: String): Boolean {
    if (!file.isFile || file.length() != size) return false
    val digest = MessageDigest.getInstance("SHA-256")
    file.inputStream().use { input ->
        val buffer = ByteArray(CHUNK)
        while (true) {
            val read = input.read(buffer)
            if (read < 0) break
            digest.update(buffer, 0, read)
        }
    }
    return digest.digest().joinToString("") { "%02x".format(it) } == sha256
}
