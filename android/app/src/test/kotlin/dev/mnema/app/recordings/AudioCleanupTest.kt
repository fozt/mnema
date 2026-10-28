package dev.mnema.app.recordings

import dev.mnema.app.processing.ProcessStage
import dev.mnema.app.processing.ProcessState
import dev.mnema.app.processing.ProcessStateFile
import dev.mnema.app.server.Connection
import dev.mnema.app.server.RunnerClient
import dev.mnema.app.server.parseConnection
import dev.mnema.app.sync.LibraryClient
import dev.mnema.app.sync.SyncController
import dev.mnema.app.sync.TestLibrary
import dev.mnema.app.sync.edit
import dev.mnema.app.sync.recording
import dev.mnema.app.sync.resource
import dev.mnema.app.sync.Checkpoint
import dev.mnema.app.sync.FileName
import dev.mnema.app.sync.Record
import dev.mnema.app.sync.Stamp
import dev.mnema.app.sync.saveCheckpoint
import dev.mnema.app.sync.sha256
import dev.mnema.app.transcript.Meta
import dev.mnema.app.transcript.WavFixture
import dev.mnema.app.ui.recordings.cleanupAsk
import java.io.File
import java.io.IOException
import java.util.UUID
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.MockWebServer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

private const val SELECTED = "http://node:8000"

class AudioCleanupTest {
    @get:Rule val tmp = TemporaryFolder()

    private val trashed = mutableListOf<String>()
    private var recordingNow: String? = null
    private val root: File get() = File(tmp.root, "Mnema").apply { mkdirs() }.canonicalFile
    private val http = RunnerClient.defaultHttp()

    // a recording with texts, fully processed by history
    private fun processed(path: String): File =
        recording(root, path).also { Meta.appendRun(it, JsonObject(mapOf("status" to JsonPrimitive("ok")))) }

    private fun withSound(folder: File, vararg tracks: String = arrayOf("mic.wav")): File {
        tracks.forEach { WavFixture.write(File(folder, it), WavFixture.samples(1.0)) }
        return folder
    }

    // own work of a recording: the id is the recording id in meta, the marker is its current history
    private fun withWork(dir: File, state: ProcessState) =
        ProcessStateFile.write(dir, state.copy(id = recordingId(dir)!!, since = outcomeKey(Meta.read(dir))))

    private fun soundBytes(folder: File): Long = folder.listFiles()!!.filter { it.extension == "wav" }.sumOf { it.length() }

    private fun store(sync: SyncController, trash: (File) -> Unit = { trashed += it.relativeTo(root).invariantSeparatorsPath; assertTrue(it.delete()) },
        hasTrash: () -> Boolean = { true }) =
        FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), { recordingNow }, { false }, Dispatchers.Unconfined,
            trash = trash, textsOnServer = sync::textsOnServer, hasTrash = hasTrash)

    private fun withServer(test: suspend (url: String, sync: (() -> Connection?) -> SyncController) -> Unit) = runBlocking {
        MockWebServer().use { server ->
            server.dispatcher = TestLibrary()
            server.start()
            val url = server.url("/").toString().trimEnd('/') + "#token=t"
            test(url) { selected -> SyncController(root, File(tmp.root, "sync.json"), selected, { emptySet() }, {}, {}, clientFactory = { LibraryClient(it, http) }) }
        }
    }

    @Test fun onlyIdleProcessedRecordingsWhoseTextsTheSelectedServerConfirmedLoseTheirSound() = withServer { url, controller ->
        val done = withSound(processed("2026-10-21_10-00"), "mic.wav", "system.wav")
        val inSection = withSound(processed("Работа/2026-10-20_Дейли"))
        val edited = withSound(processed("2026-10-21_11-00"))
        val processing = withSound(processed("2026-10-21_12-00"))
        val recorded = withSound(processed("2026-10-21_13-00"))
        val failed = withSound(processed("2026-10-21_14-00"))
        val soundOnly = withSound(File(root, "2026-10-21_15-00").apply { mkdirs() })
        var selected: Connection? = parseConnection(url)
        val sync = controller { selected }
        assertTrue(sync.runOnce().errors.isEmpty())
        val later = withSound(processed("2026-10-21_16-00"))
        edit(File(edited, "transcript.txt"), "правка после сверки", 300)
        withWork(processing, ProcessState("p", stage = ProcessStage.ASR))
        withWork(failed, ProcessState("f", stage = ProcessStage.FAILED, problem = "Узел недоступен"))
        recordingNow = recorded.name
        val store = store(sync)

        selected = parseConnection("http://other-node:8000#token=t")
        assertTrue(store.audioCleanup()!!.records.isEmpty())
        // no server, nobody confirms: processed audio goes, the texts stay only on the phone
        selected = null
        val here = store.audioCleanup()!!
        assertEquals(setOf(done, inSection, edited, later), here.records.map { it.dir }.toSet())
        assertFalse(here.onServer)
        assertTrue(cleanupAsk(here).text.endsWith("Тексты останутся только на телефоне."))
        selected = parseConnection(url)
        val plan = store.audioCleanup()!!

        assertEquals(setOf(done, inSection), plan.records.map { it.dir }.toSet())
        assertTrue(plan.onServer)
        assertEquals(soundBytes(done) + soundBytes(inSection), plan.bytes)
        assertEquals(2, store.clearAudio(plan))
        assertEquals(setOf("2026-10-21_10-00/mic.wav", "2026-10-21_10-00/system.wav", "Работа/2026-10-20_Дейли/mic.wav"), trashed.toSet())
        for (dir in listOf(done, inSection)) {
            assertEquals(setOf("transcript.txt", "summary.txt", ".mnema-meta.json"), dir.list()!!.toSet())
        }
        for (dir in listOf(edited, processing, recorded, failed, soundOnly, later)) assertTrue(dir.name, File(dir, "mic.wav").isFile)
    }

    @Test fun theChoiceIsCheckedAgainBeforeAnyFileMoves() = withServer { url, controller ->
        val edited = withSound(processed("2026-10-21_10-00"))
        val started = withSound(processed("2026-10-21_11-00"))
        val kept = withSound(processed("2026-10-21_12-00"))
        val sync = controller { parseConnection(url) }
        assertTrue(sync.runOnce().errors.isEmpty())
        val store = store(sync)
        val plan = store.audioCleanup()!!
        assertEquals(3, plan.records.size)
        val appeared = withSound(processed("2026-10-21_13-00"))
        assertTrue(sync.runOnce().errors.isEmpty())

        edit(File(edited, "transcript.txt"), "правка после выбора", 400)
        withWork(started, ProcessState("p", stage = ProcessStage.NEW))

        assertEquals(1, store.clearAudio(plan))
        assertEquals(listOf("2026-10-21_12-00/mic.wav"), trashed)
        assertTrue(File(edited, "mic.wav").isFile && File(started, "mic.wav").isFile && File(appeared, "mic.wav").isFile)
        assertEquals(setOf("transcript.txt", "summary.txt", ".mnema-meta.json"), kept.list()!!.toSet())
    }

    /**
     * Fixture shared with the desktop: one rule for the processed-audio cleanup and one recording's audio on both sides.
     * SyncController checks the texts by sync.json, as in the app; selected: null — no server selected.
     */
    @Test fun theSharedCleanupRuleDecidesWhichSoundLeaves() = runBlocking {
        val cases = Json.parseToJsonElement(resource("audio-cleanup-v1.json")).jsonObject.getValue("cases").jsonArray
        for ((i, case) in cases.map { it.jsonObject }.withIndex()) for (single in listOf(false, true)) {
            val name = case.getValue("name").jsonPrimitive.content
            val base = File(tmp.root, "case$i-$single/Mnema").apply { mkdirs() }.canonicalFile
            val folder = File(base, "2026-10-21_10-00").apply { mkdirs() }
            val id = UUID.randomUUID().toString()
            File(folder, Meta.FILE).writeText(JsonObject(mapOf("version" to JsonPrimitive(1), "id" to JsonPrimitive(id),
                "runs" to case.getValue("runs"))).toString())
            // desktop processing in progress (.mnema-processing) counts on the phone as the recording's unfinished work
            case.getValue("files").jsonObject.forEach { (file, text) ->
                if (file == ".mnema-processing") withWork(folder, ProcessState("work", stage = ProcessStage.NEW))
                else File(folder, file).writeText(text.jsonPrimitive.content)
            }
            val synced = case["synced"] as? JsonObject
            val server = case.getValue("server").jsonPrimitive.content.let { if (it == "selected") SELECTED else it }
            val checkpoint = if (synced == null) Checkpoint(server = server) else Checkpoint(server = server, records = mapOf(id to Record(
                id, folder.name, synced.entries.associate { (file, text) ->
                    FileName.entries.single { it.fileName == file } to Stamp(sha256(text.jsonPrimitive.content.toByteArray()), 0)
                }, "")))
            val state = File(base.parentFile, "sync.json").also { saveCheckpoint(it, checkpoint) }
            val selected = if (case["selected"] is JsonNull) null else parseConnection("$SELECTED#token=t")
            val sync = SyncController(base, state, { selected }, { emptySet() }, {}, {})
            val removed = mutableListOf<String>()
            val store = FolderRecordingStore(base, CoroutineScope(Dispatchers.Unconfined), { null }, { false }, Dispatchers.Unconfined,
                trash = { removed += it.name; assertTrue(it.delete()) }, textsOnServer = sync::textsOnServer)

            val expected = case.getValue("removed").jsonArray.map { it.jsonPrimitive.content }

            if (single) {
                assertEquals(name, expected.isNotEmpty(), store.detail(folder)!!.cleanable)
                assertEquals(name, expected.isNotEmpty(), store.clearAudio(folder))
            } else store.clearAudio(store.audioCleanup()!!)

            assertEquals(name, expected, removed.sorted())
        }
    }

    @Test fun withoutTrashOnlyAPlanConfirmedWithoutItDeletesSoundForever() = withServer { url, controller ->
        val done = withSound(processed("2026-10-21_10-00"))
        val sync = controller { parseConnection(url) }
        assertTrue(sync.runOnce().errors.isEmpty())
        var bin = true
        val store = store(sync, hasTrash = { bin })
        val toTrash = store.audioCleanup()!!
        assertTrue(toTrash.trash)
        bin = false

        assertEquals(0, store.clearAudio(toTrash))
        assertEquals("Корзина недоступна", store.failure.value)
        assertTrue(File(done, "mic.wav").isFile)
        val forever = store.audioCleanup()!!
        assertFalse(forever.trash)
        assertEquals(1, store.clearAudio(forever))
        assertTrue(trashed.isEmpty())
        assertEquals(setOf("transcript.txt", "summary.txt", ".mnema-meta.json"), done.list()!!.toSet())
    }

    @Test fun withoutTheSystemTrashSoundStaysAndTheReasonIsShown() = withServer { url, controller ->
        val done = withSound(processed("2026-10-21_10-00"))
        val sync = controller { parseConnection(url) }
        assertTrue(sync.runOnce().errors.isEmpty())
        val store = store(sync, trash = { throw IOException("Для системной корзины нужен Android 17") })

        assertEquals(0, store.clearAudio(store.audioCleanup()!!))
        assertEquals("Для системной корзины нужен Android 17", store.failure.value)
        assertTrue(File(done, "mic.wav").isFile)
    }
}
