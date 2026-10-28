package dev.mnema.app.processing

import dev.mnema.app.settings.NodeEntry
import dev.mnema.app.settings.Settings
import dev.mnema.app.server.RunnerClient
import dev.mnema.app.server.parseConnection
import kotlinx.coroutines.runBlocking
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertNull
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test

class RunnerCancelTest {
    private val server = MockWebServer()
    private val http = OkHttpClient()

    @Before
    fun setUp() = server.start()

    @After
    fun tearDown() = server.close()

    @Test
    fun jobIsCancelledAndAFailureIsSwallowed() = runBlocking {
        server.enqueue(MockResponse(500, body = "{}"))
        val runner = RunnerClient(parseConnection(server.url("/").toString().trimEnd('/') + "#token=t"), http)

        cancelJob(runner, "a")

        assertEquals("POST /api/jobs/a/cancel", server.takeRequest().let { "${it.method} ${it.target}" })
    }

    @Test
    fun unreachableServerIsNotAnError() = runBlocking {
        val runner = RunnerClient(parseConnection(server.url("/").toString().trimEnd('/')), http)
        server.close()

        cancelJob(runner, "a")
    }
    @Test fun cancellationUsesTheOriginalNodeAndNeverFallsBackToActive() {
        val original = NodeEntry("http://old#token=old", "Старый")
        val selected = NodeEntry("http://new#token=new", "Новый")
        val settings = Settings(nodes = listOf(original, selected), activeNode = selected.address)
        assertEquals(original, cancellationNode(settings, original.address))
        assertNull(cancellationNode(settings.withoutNode(original.address), original.address))
    }

}
