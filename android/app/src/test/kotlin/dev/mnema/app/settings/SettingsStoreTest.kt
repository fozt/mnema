package dev.mnema.app.settings

import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import dev.mnema.app.model.Lang
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class SettingsStoreTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private lateinit var file: File
    private lateinit var store: SettingsStore

    @Before
    fun setUp() {
        file = File(folder.root, "settings.preferences_pb")
        store = SettingsStore(PreferenceDataStoreFactory.create(scope = scope) { file })
    }

    @After
    fun tearDown() = scope.cancel()

    @Test
    fun emptyStoreGivesDefaults() = runBlocking {
        assertEquals(Settings(), store.current())
    }

    @Test
    fun updatePersistsTheNodeAndJobOptions() = runBlocking {
        val full = Settings(
            nodes = listOf(NodeEntry("http://192.168.0.10:8000#token=abc", "Сервер")),
            activeNode = "http://192.168.0.10:8000",
            lang = Lang.RU,
            asr = "groq/whisper-large-v3-turbo",
            onboardingDone = true,
            themeMode = ThemeMode.DARK,
            colorSource = ColorSource.WALLPAPER,
            summaryLang = SummaryLang.EN,
            recordMic = false,
            recordCall = false,
            recordSection = "Работа",
            updateCheckedAt = 1_792_612_008_754,
            playbackSpeed = 1.75f,
        )

        store.update { full }
        assertEquals(full, store.current())

        store.update { it.copy(lang = Lang.EN) }
        assertEquals("other settings are not reset", full.copy(lang = Lang.EN), store.current())
        assertEquals("http://192.168.0.10:8000#token=abc", store.current().connection)
    }

    @Test
    fun printedSettingsHideTokensOfEveryNode() {
        val nodes = listOf(
            NodeEntry("http://host:8000#token=secret-token", "Сервер"),
            NodeEntry("http://pc:8000#token=pc-token", "ПК"),
        )
        val text = Settings(nodes = nodes, activeNode = "http://host:8000").toString()

        assertFalse(text, "secret-token" in text)
        assertFalse(text, "pc-token" in text)
        assertTrue(text, "Сервер" in text)
    }

    @Test
    fun firstAddedNodeBecomesActiveAndLaterOnesWaitForATap() {
        val server = NodeEntry("http://box:8000#token=a", "Сервер")
        val pc = NodeEntry("http://pc:8000#token=b", "ПК")

        val one = Settings().withNode(server)
        assertEquals("http://box:8000", one.activeNode)

        val two = one.withNode(pc)
        assertEquals(listOf(server, pc), two.nodes)
        assertEquals("http://box:8000", two.activeNode)
        assertEquals("http://pc:8000", two.withActive("http://pc:8000").activeNode)
        assertEquals("an unknown address is not selected", two, two.withActive("http://other:1"))
    }

    @Test
    fun addingAKnownAddressReplacesItsTokenInPlace() {
        val settings = Settings()
            .withNode(NodeEntry("http://box:8000#token=revoked", "Сервер"))
            .withNode(NodeEntry("http://pc:8000#token=b", "ПК"))

        val again = settings.withNode(NodeEntry("http://box:8000#token=fresh", "Сервер"))

        assertEquals(listOf("http://box:8000#token=fresh", "http://pc:8000#token=b"), again.nodes.map { it.connection })
        assertEquals("http://box:8000", again.activeNode)
    }

    @Test
    fun removingTheActiveNodeLeavesNoneActive() {
        val settings = Settings()
            .withNode(NodeEntry("http://box:8000#token=a", "Сервер"))
            .withNode(NodeEntry("http://pc:8000#token=b", "ПК"))

        val removed = settings.withoutNode("http://box:8000")

        assertEquals(listOf("ПК"), removed.nodes.map { it.name })
        assertEquals("", removed.activeNode)
        assertEquals("", removed.connection)
        assertEquals("http://box:8000", settings.withoutNode("http://pc:8000").activeNode)
    }

    @Test
    fun nodeNameFollowsWhatTheNodeReports() {
        val settings = Settings().withNode(NodeEntry("http://box:8000#token=a", "box"))

        assertEquals(listOf("Сервер"), settings.withNodeName("http://box:8000", "Сервер").nodes.map { it.name })
        assertEquals(settings, settings.withNodeName("http://box:8000", " "))
        assertEquals(settings, settings.withNodeName("http://other:1", "Другой"))
    }
}
