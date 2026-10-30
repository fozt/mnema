package dev.mnema.app.update

import kotlinx.coroutines.runBlocking
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

class UpdateCheckerTest {
    private val server = MockWebServer()
    private val http = OkHttpClient()
    private val sha = "b".repeat(64)
    private val body = """{"version": "0.2.9", "version_code": 209, "apk": "https://github.com/fozt/mnema/releases/download/v0.2.9/mnema-0.2.9.apk",
        |"sha256": "$sha", "size": 15000000, "min_sdk": 34}""".trimMargin()

    @Before
    fun setUp() = server.start()

    @After
    fun tearDown() {
        server.close()
        http.dispatcher.executorService.shutdown()
        http.connectionPool.evictAll()
    }

    private fun url() = server.url("/packages/generic/mnema/latest/mnema-android.json").toString()

    private fun failure(url: String = url()): String {
        try {
            runBlocking { fetchManifest(http, url) }
        } catch (e: UpdateException) {
            return e.message.orEmpty()
        }
        fail("manifest received")
        throw AssertionError()
    }

    @Test
    fun manifestIsFetchedWithAnAnonymousGet() = runBlocking {
        server.enqueue(MockResponse.Builder().code(200).body(body).build())

        val manifest = fetchManifest(http, url())

        assertEquals("0.2.9", manifest.version)
        assertEquals(209L, manifest.versionCode)
        assertEquals(15_000_000L, manifest.size)
        val request = server.takeRequest()
        assertEquals("GET", request.method)
        assertEquals("/packages/generic/mnema/latest/mnema-android.json", request.target)
        assertEquals(null, request.headers["Authorization"])
    }

    @Test
    fun closedOrMissingRegistryIsNotPublished() {
        listOf(401, 403, 404).forEach { code ->
            server.enqueue(MockResponse.Builder().code(code).build())
            assertEquals("Обновления сейчас недоступны", failure())
        }
    }

    @Test
    fun serverErrorOrBrokenManifestCannotBeChecked() {
        server.enqueue(MockResponse.Builder().code(500).build())
        assertEquals("Не удалось проверить обновления. Повторите позже", failure())

        server.enqueue(MockResponse.Builder().code(200).body("<html>").build())
        assertEquals("Не удалось проверить обновления. Повторите позже", failure())
    }

    @Test
    fun unreachableChannelIsNoConnection() {
        val gone = url()
        server.close()

        assertEquals("Нет связи", failure(gone))
    }

    @Test
    fun checkIsDueEveryTwelveHours() {
        val hours12 = 12 * 60 * 60 * 1000L
        val now = 1_800_000_000_000L

        assertTrue(due(checkedAt = 0, now = now))
        assertFalse(due(checkedAt = now - 1000, now = now))
        assertFalse(due(checkedAt = now - hours12 + 1, now = now))
        assertTrue(due(checkedAt = now - hours12, now = now))
        assertTrue(due(checkedAt = now + hours12, now = now))
    }

    @Test
    fun foregroundChecksWhenDueAndNothingIsUnderWay() {
        val hours12 = 12 * 60 * 60 * 1000L
        val checkedAt = 1_800_000_000_000L
        val manifest = parseManifest(body)
        val apk = java.io.File("/cache/updates/mnema-0.2.9.apk")
        val resting = listOf(
            UpdateStage.Idle,
            UpdateStage.UpToDate,
            UpdateStage.Unsuitable(manifest),
            UpdateStage.Available(manifest),
            UpdateStage.Failed("Нет связи"),
        )
        val busy = listOf(
            UpdateStage.Checking,
            UpdateStage.Downloading(manifest, 0.5f),
            UpdateStage.Ready(manifest, apk),
            UpdateStage.NeedsPermission(manifest, apk),
            UpdateStage.Installing(manifest, apk),
            UpdateStage.Confirm(manifest, apk, 6),
            UpdateStage.Failed("Файл обновления повреждён", manifest),
        )

        for (stage in resting) {
            assertFalse("$stage", checksOnForeground(stage, checkedAt, now = checkedAt + hours12 - 1))
            assertTrue("$stage", checksOnForeground(stage, checkedAt, now = checkedAt + hours12))
        }
        for (stage in busy) assertFalse("$stage", checksOnForeground(stage, checkedAt, now = checkedAt + hours12))
    }
}
