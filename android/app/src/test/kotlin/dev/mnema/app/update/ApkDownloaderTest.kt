package dev.mnema.app.update

import java.io.File
import java.security.MessageDigest
import kotlinx.coroutines.runBlocking
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.OkHttpClient
import okio.Buffer
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class ApkDownloaderTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val server = MockWebServer()
    private val http = OkHttpClient()
    private val bytes = ByteArray(200_000) { (it * 31 % 251).toByte() }

    @Before
    fun setUp() = server.start()

    @After
    fun tearDown() {
        server.close()
        http.dispatcher.executorService.shutdown()
        http.connectionPool.evictAll()
    }

    private fun sha(data: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) }

    private fun manifest(size: Long = bytes.size.toLong(), sha256: String = sha(bytes)) = UpdateManifest(
        version = "0.2.9",
        versionCode = 209,
        apk = server.url("/mnema-0.2.9.apk").toString(),
        sha256 = sha256,
        size = size,
        minSdk = 34,
    )

    private fun updatesDir() = File(folder.root, "updates")

    private fun downloader() = ApkDownloader(http, updatesDir())

    private fun failure(block: suspend () -> Unit): String {
        try {
            runBlocking { block() }
        } catch (e: UpdateException) {
            return e.message.orEmpty()
        }
        fail("the download succeeded")
        throw AssertionError()
    }

    private fun apkReply(data: ByteArray = bytes) = MockResponse.Builder().code(200).body(Buffer().write(data)).build()

    @Test
    fun downloadSavesTheApkUnderItsVersionAndReportsProgress() = runBlocking {
        server.enqueue(apkReply())
        val progress = mutableListOf<Float>()

        val file = downloader().download(manifest()) { progress += it }

        assertEquals(File(updatesDir(), "mnema-0.2.9.apk"), file)
        assertArrayEquals(bytes, file.readBytes())
        assertEquals("/mnema-0.2.9.apk", server.takeRequest().target)
        assertEquals(1f, progress.last())
        assertEquals(progress.sorted(), progress)
        assertEquals(listOf("mnema-0.2.9.apk"), updatesDir().list()?.toList())
    }

    @Test
    fun damagedDownloadIsReportedAndDeleted() {
        val other = bytes.copyOf().also { it[100] = (it[100] + 1).toByte() }
        // another hash and a file longer than the manifest
        for (data in listOf(other, bytes + bytes)) {
            server.enqueue(apkReply(data))

            assertEquals("${data.size} bytes", "Файл обновления повреждён", failure { downloader().download(manifest()) {} })
            assertEquals(emptyList<String>(), updatesDir().list()?.toList())
        }
    }

    @Test
    fun alreadyDownloadedFileIsReusedAndOlderOnesAreRemoved() = runBlocking {
        updatesDir().mkdirs()
        File(updatesDir(), "mnema-0.2.9.apk").writeBytes(bytes)
        File(updatesDir(), "mnema-0.2.8.apk").writeBytes(byteArrayOf(1, 2, 3))

        val file = downloader().download(manifest()) {}

        assertArrayEquals(bytes, file.readBytes())
        assertEquals(0, server.requestCount)
        assertEquals(listOf("mnema-0.2.9.apk"), updatesDir().list()?.toList())
    }

    @Test
    fun missingApkInTheRegistryIsNotPublished() {
        server.enqueue(MockResponse.Builder().code(404).build())

        assertEquals(
            "Обновления сейчас недоступны",
            failure { downloader().download(manifest()) {} },
        )

        val gone = manifest()
        server.close()
        assertEquals("Нет связи с сервером обновлений", failure { downloader().download(gone) {} })
    }
}
