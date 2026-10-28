package dev.mnema.app.sync

import dev.mnema.app.recordings.FolderRecordingStore
import dev.mnema.app.server.RunnerClient
import dev.mnema.app.server.parseConnection
import dev.mnema.app.settings.NodeEntry
import dev.mnema.app.settings.Settings
import dev.mnema.app.ui.recordings.RecordingsViewModel
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.time.ZoneId
import java.util.UUID
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.withTimeout
import mockwebserver3.MockWebServer
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class SyncControllerTest {
    @get:Rule val tmp = TemporaryFolder()

    @Test fun theChosenServerSyncsSingleTrashBulkRestoreAndServerSwitch(): Unit = runBlocking {
        val library = TestLibrary()
        MockWebServer().use { server ->
            server.dispatcher = library
            server.start()
            MockWebServer().use { other ->
                other.start()
                val home = NodeEntry(server.url("/").toString().trimEnd('/') + "#token=home", "Дом")
                val otherNode = NodeEntry(other.url("/").toString().trimEnd('/') + "#token=other", "Другой")
                var settings = Settings(nodes = listOf(home, otherNode), activeNode = home.address)
                val root = tmp.newFolder("client")
                val checkpoint = File(tmp.root, "sync.json")
                val trash: (File) -> Unit = { Files.move(it.toPath(), File(tmp.root, "native-trash-${UUID.randomUUID()}").toPath()) }
                val store = FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), { null }, { false }, Dispatchers.Unconfined, trash = trash)
                var changed = 0
                val http = RunnerClient.defaultHttp()
                try {
                    val controller = SyncController(root, checkpoint, { settings.activeEntry()?.let { parseConnection(it.connection) } }, { emptySet() }, trash,
                        changed = { changed++; store.refresh() }, clientFactory = { LibraryClient(it, http) })
                    val one = library.add("Работа/one")
                    library.sections += "Пустой"
                    assertTrue(controller.runOnce().errors.isEmpty())
                    assertEquals(1, changed)
                    assertEquals(listOf("Пустой", "Работа"), store.sections.value)
                    assertEquals(one, store.items.value.single().id)
                    assertEquals(0, other.requestCount)
                    assertTrue(store.delete(store.items.value.single().dir))
                    assertTrue(controller.runOnce().errors.isEmpty())
                    assertFalse(one in library.records)
                    val two = library.add("Работа/two")
                    val three = library.add("three")
                    controller.runOnce()
                    for (item in store.items.value) assertTrue(store.delete(item.dir))
                    val pending = controller.runOnce()
                    assertEquals(setOf(two, three), pending.bulkMissing.toSet())
                    assertEquals(setOf(two, three), library.records.keys)
                    var interrupted = false
                    library.before = { request ->
                        if (request.method == "GET" && request.target.contains("/files/")) {
                            assertFalse(two in loadCheckpoint(checkpoint).records)
                            assertFalse(three in loadCheckpoint(checkpoint).records)
                            interrupted = true
                            response(503)
                        } else null
                    }
                    assertTrue(controller.resolveBulk(pending.bulkMissing, true).errors.isNotEmpty())
                    assertTrue(interrupted)
                    library.before = { null }
                    assertTrue(controller.runOnce().errors.isEmpty())
                    store.refresh()
                    assertEquals(setOf(two, three), store.items.value.map { it.id }.toSet())
                    for (item in store.items.value) assertTrue(store.delete(item.dir))
                    val secondPending = controller.runOnce()
                    settings = settings.withActive(otherNode.address)
                    assertTrue(controller.resolveBulk(secondPending.bulkMissing, false).errors.isNotEmpty())
                    assertEquals(0, other.requestCount)
                    assertEquals(setOf(two, three), library.records.keys)
                    settings = settings.withoutNode(otherNode.address)
                    assertTrue(controller.runOnce().bulkMissing.isEmpty())
                    assertEquals(0, other.requestCount)
                } finally { http.dispatcher.executorService.shutdown(); http.connectionPool.evictAll() }
                library.failure?.let { throw AssertionError(it) }
            }
        }
    }

    @OptIn(ExperimentalCoroutinesApi::class)
    @Test fun withoutTrashTheListChoiceDeletesARecordingRemovedElsewhereForever() {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        try {
            runBlocking {
                val library = TestLibrary()
                MockWebServer().use { server ->
                    server.dispatcher = library
                    server.start()
                    val connection = parseConnection(server.url("/").toString().trimEnd('/') + "#token=t")
                    val root = tmp.newFolder("client")
                    val http = RunnerClient.defaultHttp()
                    try {
                        val store = FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), { null }, { false }, Dispatchers.Unconfined,
                            trash = { error("unexpected trash") }, hasTrash = { false })
                        val sync = SyncController(root, File(tmp.root, "sync.json"), { connection }, { emptySet() }, { error("unexpected trash") }, {},
                            clientFactory = { LibraryClient(it, http) }, hasTrash = { false })
                        val id = library.add("Встреча")
                        assertTrue(sync.runOnce().errors.isEmpty())
                        val folder = File(root, "Встреча")
                        assertTrue(folder.isDirectory)
                        library.records.remove(id)
                        assertEquals(listOf(id), sync.runOnce().serverMissing)
                        assertTrue(folder.isDirectory)

                        val model = RecordingsViewModel(store, ZoneId.of("UTC"), sync)
                        model.resolveBulk(restore = false, onServer = true)
                        model.resolving.first { !it }
                        assertFalse(folder.exists())
                        assertEquals(emptyList<String>(), sync.status.value.errors)
                        assertTrue(sync.status.value.serverMissing.isEmpty())
                    } finally { http.dispatcher.executorService.shutdown(); http.connectionPool.evictAll() }
                }
                library.failure?.let { throw AssertionError(it) }
            }
        } finally { Dispatchers.resetMain() }
    }

    /** Selecting a server does not wait for a running sync: the new library folder appears at once and sync goes on. */
    @Test fun choosingAServerDoesNotWaitForARunningSync(): Unit = runBlocking {
        val root = File(tmp.root, "Mnema")
        val started = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        val controller = SyncController(root, File(tmp.root, "sync.json"), { parseConnection("http://unused:8000") }, { emptySet() }, {}, {},
            prepare = { started.complete(Unit); release.await(); throw IOException("Сверка остановлена") }, clientFactory = { error("HTTP must not start") })
        val running = async(Dispatchers.Default) { controller.runOnce() }
        try {
            started.await()
            withTimeout(5_000) { controller.initializeLibrary() }
            assertTrue(root.isDirectory)
            assertFalse(running.isCompleted)
        } finally { release.complete(Unit) }
        assertEquals(listOf("Сверка остановлена"), running.await().errors)
    }

    @Test fun helperPreparationFailureIsVisibleAndDoesNotStartHttp(): Unit = runBlocking {
        val root = tmp.newFolder("client")
        var clients = 0
        val controller = SyncController(root, File(tmp.root, "sync.json"), { parseConnection("http://unused:8000") }, { emptySet() }, {}, {},
            prepare = { throw IOException("Неизвестно состояние записи") }, clientFactory = { clients++; error("HTTP must not start") })
        assertEquals(listOf("Неизвестно состояние записи"), controller.runOnce().errors)
        assertEquals(0, clients)
        assertFalse(File(tmp.root, "sync.json").exists())
    }
}
