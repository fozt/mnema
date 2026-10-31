package dev.mnema.app.ui

import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import dev.mnema.app.server.Connection
import dev.mnema.app.server.RunnerClient
import dev.mnema.app.settings.NodeEntry
import dev.mnema.app.settings.SettingsStore
import dev.mnema.app.ui.settings.JoinResult
import dev.mnema.app.ui.settings.joinNode
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class NodeJoinTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val node = MockWebServer()
    private val http: OkHttpClient = RunnerClient.defaultHttp()
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private lateinit var file: File
    private lateinit var store: SettingsStore
    private lateinit var address: String
    private val code = "Zq3-invite-code_9"
    private val capabilities = """{"ok":true,"llm":true,"name":"ПК Ubuntu"}"""

    @Before
    fun setUp() {
        node.start()
        address = node.url("/").toString().trimEnd('/')
        file = File(folder.root, "settings.preferences_pb")
        store = SettingsStore(PreferenceDataStoreFactory.create(scope = scope) { file })
    }

    @After
    fun tearDown() {
        node.close()
        scope.cancel()
        http.dispatcher.executorService.shutdown()
        http.connectionPool.evictAll()
    }

    private fun reply(code: Int, body: String = "") = MockResponse.Builder().code(code).body(body).build()

    private val joined = mutableListOf<String>()

    private fun join(text: String) = runBlocking {
        joinNode(text, "Pixel 9", { connection: Connection -> RunnerClient(connection, http) }, store) { joined += it }
    }

    @Test
    fun inviteIsExchangedForADeviceTokenThatIsSavedWithoutTheCode() = runBlocking<Unit> {
        node.enqueue(reply(200, """{"token":"device-token-1","device":{"id":"d1","name":"Pixel 9"}}"""))
        node.enqueue(reply(200, capabilities))

        val result = join("$address#invite=$code")

        assertEquals(JoinResult.Added(address), result)
        val saved = store.current()
        assertEquals(listOf(NodeEntry("$address#token=device-token-1", "ПК Ubuntu")), saved.nodes)
        assertEquals(address, saved.activeNode)
        assertFalse("кода приглашения нет в настройках", file.readBytes().toString(Charsets.ISO_8859_1).contains(code))
        assertFalse(saved.toString().contains("device-token-1"))
        val claim = node.takeRequest()
        assertEquals("/api/devices/claim", claim.target)
        assertNull(claim.headers["Authorization"])
        assertTrue(claim.body!!.utf8().contains("Pixel 9"))
        assertEquals("Bearer device-token-1", node.takeRequest().headers["Authorization"])
    }

    @Test
    fun claimedTokenIsKeptEvenIfTheNodeThenDoesNotAnswer() = runBlocking<Unit> {
        node.enqueue(reply(200, """{"token":"device-token-1","device":{"id":"d1","name":"Pixel 9"}}"""))
        node.enqueue(reply(503))

        val result = join("$address#invite=$code")

        assertEquals(JoinResult.Added(address), result)
        val host = node.url("/").host
        assertEquals(listOf(NodeEntry("$address#token=device-token-1", host)), store.current().nodes)
    }

    @Test
    fun refusedInviteShowsTheNodeTextAndSavesNothing() = runBlocking<Unit> {
        node.enqueue(reply(403, """{"detail":"Приглашение недействительно: попросите новое на узле"}"""))

        val result = join("$address#invite=$code")

        assertEquals(JoinResult.Refused("Приглашение недействительно: попросите новое на узле"), result)
        assertEquals(emptyList<NodeEntry>(), store.current().nodes)
        assertEquals(1, node.requestCount)
    }

    @Test
    fun openServerAddressIsCheckedAndNamedByTheNode() = runBlocking<Unit> {
        node.enqueue(reply(200, capabilities))

        val result = join(" $address/ ")

        assertEquals(JoinResult.Added(address), result)
        assertEquals(listOf(NodeEntry(address, "ПК Ubuntu")), store.current().nodes)
        val check = node.takeRequest()
        assertEquals("/api/capabilities", check.target)
        assertNull(check.headers["Authorization"])
    }

    @Test
    fun addressOfAServerWithATokenIsRefusedAndNotSaved() = runBlocking<Unit> {
        node.enqueue(reply(401))

        assertEquals(JoinResult.Refused("Вставьте приглашение с сервера"), join(address))
        assertEquals(emptyList<NodeEntry>(), store.current().nodes)
    }

    @Test
    fun secondNodeDoesNotTakeOverTheActiveOne() = runBlocking<Unit> {
        store.update { it.withNode(NodeEntry("http://box:8000#token=a", "Сервер")) }
        node.enqueue(reply(200, """{"token":"device-token-1","device":{"id":"d1","name":"Pixel 9"}}"""))
        node.enqueue(reply(200, capabilities))

        join("$address#invite=$code")

        assertEquals(listOf("Сервер", "ПК Ubuntu"), store.current().nodes.map { it.name })
        assertEquals("http://box:8000", store.current().activeNode)
    }

    @Test
    fun onlyANewTokenForAnAddressLeavesItsLastSyncUnchecked() = runBlocking<Unit> {
        store.update { it.withNode(NodeEntry("$address#token=old", "Сервер")) }
        node.enqueue(reply(200, """{"token":"device-token-1","device":{"id":"d1","name":"Pixel 9"}}"""))
        node.enqueue(reply(200, capabilities))
        join("$address#invite=$code")
        assertEquals(listOf(address), joined)

        node.enqueue(reply(200, capabilities))
        join(address)
        assertEquals(listOf(address, address), joined) // an open server where the device had a token
        node.enqueue(reply(200, capabilities))
        join(address)
        assertEquals(listOf(address, address), joined) // the same open server again
    }

    @Test
    fun wrongLineIsRefusedBeforeAnyRequest() = runBlocking<Unit> {
        assertEquals(JoinResult.Refused("Вставьте приглашение с сервера"), join("  "))
        assertEquals(JoinResult.Refused("Вставьте приглашение с сервера"), join("$address#token=old-token"))
        assertEquals(
            JoinResult.Refused("Неверное приглашение. Скопируйте его с сервера целиком"),
            join("$address#token=a&invite=b"),
        )
        assertEquals(0, node.requestCount)
        assertEquals(emptyList<NodeEntry>(), store.current().nodes)
    }
}
