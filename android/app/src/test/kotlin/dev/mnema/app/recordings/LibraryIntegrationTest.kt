package dev.mnema.app.recordings

import dev.mnema.app.processing.ProcessStage
import dev.mnema.app.processing.ProcessState
import dev.mnema.app.processing.ProcessStateFile
import dev.mnema.app.processing.WorkScheduler
import dev.mnema.app.transcript.Meta
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.util.TimeZone
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import dev.mnema.app.sync.recording
import dev.mnema.app.sync.resource
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class LibraryIntegrationTest {
    @get:Rule val tmp = TemporaryFolder()
    private val root get() = File(tmp.root, "Mnema").apply { mkdirs() }
    // own work of a recording: the id is the recording id in meta, the marker is its current history
    private fun ownWork(dir: File, stage: String) =
        ProcessStateFile.write(dir, ProcessState(id = recordingId(dir)!!, stage = stage, since = outcomeKey(Meta.read(dir))))

    private fun record(path: String, id: String = path) = File(root, path).apply {
        mkdirs()
        File(this, "transcript.txt").writeText("mic: привет")
        Meta.ensureRecording(this, id) { Meta.isoNow(1_792_612_008_754, TimeZone.getTimeZone("UTC")) }
    }

    // a recording processed by history; the store's textsOnServer confirms the texts on the sync server
    private fun processed(path: String, id: String) = record(path, id).also { Meta.appendRun(it, JsonObject(mapOf("status" to JsonPrimitive("ok")))) }

    @Test fun sectionsKeepEmptyGroupsAndShareContainmentWithScheduler() {
        val nested = record("Работа/Дейли", "record-id")
        File(root, "Пустой").mkdir()
        val flat = record("Звонок")
        val layout = libraryFolders(root)
        assertEquals(setOf(nested, flat), layout.records.toSet())
        assertEquals(listOf("Пустой", "Работа"), layout.sections)
        assertTrue(WorkScheduler.accepts(nested, root, true))
        assertFalse(WorkScheduler.accepts(File(nested, "внутри").apply { mkdir(); File(this, "transcript.txt").writeText("нет") }, root, true))
        assertEquals(nested, WorkScheduler.findFolder(root, "record-id"))
        assertEquals(nested, findRecording(root, "record-id"))
        assertNull(WorkScheduler.findFolder(root, "process-id"))
        assertNull(recordingDirectory(root, tmp.root))
    }

    private fun store(sections: Set<String> = emptySet(), syncing: Boolean = false, trash: (File) -> Unit = { error("unexpected trash") }) =
        FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), { null }, { false }, Dispatchers.Unconfined,
            trash = trash, knownSections = { sections }, syncing = { syncing })

    @Test fun listFollowsTheSharedLayoutFixtureAndNeverWritesMetaIntoSections() {
        val cases = Json.parseToJsonElement(resource("library-layout-v1.json")).jsonObject.getValue("cases").jsonArray.map { it.jsonObject }
        for (case in cases) {
            val name = case.getValue("name").jsonPrimitive.content
            fun strings(key: String) = case[key]?.jsonArray.orEmpty().map { it.jsonPrimitive.content }
            root.deleteRecursively()
            for (path in strings("records")) recording(root, path).also { folder ->
                // record_files instead of texts: a recording with audio of any format is listed too
                if ("record_files" in case) { File(folder, "transcript.txt").delete(); File(folder, "summary.txt").delete() }
                strings("record_files").forEach { File(folder, it).writeBytes(ByteArray(32044)) }
            }
            for (path in strings("tree")) {
                val target = File(root, path)
                if (path.endsWith("/")) target.mkdirs()
                else target.apply { parentFile!!.mkdirs() }.writeBytes(if (target.extension in setOf("wav", "m4a")) ByteArray(32044) else "x".toByteArray())
            }
            val records = strings("records")
            // the list skips a recording inside a recording and anything deeper than a section
            val shown = records.filter { path -> path.count { it == '/' } <= 1 && records.none { path.startsWith("$it/") } }
            val known = strings("known_sections").toSet()
            val layout = libraryFolders(root, known)
            assertEquals(name, shown.sorted(), layout.records.map { it.relativeTo(root.canonicalFile).invariantSeparatorsPath }.sorted())
            if (case["complete"]?.jsonPrimitive?.boolean != false) assertEquals(name, strings("sections"), layout.sections)
            val store = store(known)
            store.refresh()
            assertEquals(name, shown.sorted(), store.items.value.map { it.dir.relativeTo(root.canonicalFile).invariantSeparatorsPath }.sorted())
            val metas = root.walkTopDown().filter { it.name == Meta.FILE }.map { it.parentFile!!.relativeTo(root).invariantSeparatorsPath }
            assertEquals(name, records.sorted(), metas.sorted().toList())
        }
    }

    @Test fun foldersRenamedDuringTheWalkNeverBreakTheListOrBusySet() {
        val stable = record("Работа/Дейли", "stable-id")
        ownWork(stable, ProcessStage.NEW)
        val moving = (0 until 20).map { "Звонок $it" } + "Работа/Переезд"
        moving.forEach { record(it) }
        val stop = AtomicBoolean(false)
        val mover = thread {
            while (!stop.get()) for (path in moving) {
                val (a, b) = File(root, path) to File(root, "$path-новое")
                if (!a.renameTo(b)) b.renameTo(a)
            }
        }
        try {
            repeat(300) {
                assertTrue(stable in libraryFolders(root).records)
                assertTrue("stable-id" in busyRecordings(root, null, { false }))
            }
        } finally { stop.set(true); mover.join() }
    }

    @Test fun unreadableRecordInASectionIsSkippedButUnreadableRootFolderIsAnError() {
        val kept = record("Работа/Дейли", "kept-id")
        ownWork(kept, ProcessStage.NEW)
        val locked = record("Работа/Закрытая", "locked-id")
        val section = File(root, "Работа")
        val rights = Files.getPosixFilePermissions(section.toPath())
        try {
            Files.setPosixFilePermissions(locked.toPath(), emptySet())
            assertEquals(listOf(kept), libraryFolders(root).records)
            assertTrue("kept-id" in busyRecordings(root, null, { false }))
            Files.setPosixFilePermissions(section.toPath(), emptySet()) // an unreadable section is not an empty library
            assertThrows(IOException::class.java) { libraryFolders(root) }
        } finally {
            Files.setPosixFilePermissions(section.toPath(), rights)
            Files.setPosixFilePermissions(locked.toPath(), rights)
        }
    }

    @Test fun strayFilesInASectionKeepItsRecordsAndTheSectionCannotBeDeleted() = runBlocking {
        val rec = record("Работа/rec", "rec-id")
        val section = File(root, "Работа")
        File(section, "memo.wav").writeBytes(ByteArray(32044))
        File(section, "transcript.txt").writeText("случайный файл")
        val store = store()
        store.refresh()
        assertEquals(listOf(rec), store.items.value.map { it.dir })
        assertEquals(listOf("Работа"), store.sections.value)
        assertFalse(File(section, Meta.FILE).exists())
        assertNull(store.detail(section))
        assertFalse(store.delete(section))
        assertTrue(rec.isDirectory)
        assertEquals(rec, findRecording(root, "rec-id"))
        File(root, ".obsidian/plugins").mkdirs()
        val empty = File(root, "Пустой").apply { mkdir() }
        File(empty, "transcript.txt").writeText("случайный файл")
        val known = store(setOf("Пустой"))
        known.refresh()
        assertEquals(listOf("Пустой", "Работа"), known.sections.value)
        assertEquals(listOf(rec), known.items.value.map { it.dir })
        assertFalse(File(empty, Meta.FILE).exists())
    }

    @Test fun copiedFolderGetsNewIdUnlessSyncSeparatesCopies() {
        val original = record("Работа/Дейли", "11111111-1111-1111-1111-111111111111")
        Thread.sleep(20) // the copy's ctime is surely later
        val copy = File(root, "Дейли").also { original.copyRecursively(it) }
        store(syncing = true).refresh()
        assertEquals(recordingId(original), recordingId(copy))
        assertNull(findRecording(root, "11111111-1111-1111-1111-111111111111"))
        store().refresh()
        assertEquals("11111111-1111-1111-1111-111111111111", recordingId(original))
        assertNotEquals(recordingId(original), recordingId(copy))
        assertEquals(original, findRecording(root, "11111111-1111-1111-1111-111111111111"))
    }

    @Test fun busyIncludesQueuedOrUnknownAndActiveWorkerAfterCancel() {
        val queued = record("Работа/queued", "queued-id")
        ownWork(queued, ProcessStage.NEW)
        val unknown = record("unknown")
        File(unknown, ProcessStateFile.FILE).writeText("broken")
        val stopped = record("stopped", "stopped-id")
        ProcessStateFile.write(stopped, ProcessState(id = "cancelled", stage = ProcessStage.STOPPED))
        val live = record("live")
        val result = busyRecordings(root, "live", { false }, setOf("stopped-id"))
        assertTrue(result.containsAll(listOf("queued-id", "Работа/queued", queued.path, unknown.path, "stopped-id", live.path)))
        assertFalse(busyRecordings(root, null, { false }).contains("stopped-id"))
    }

    @Test fun workFileOfAnotherRecordingIsNotThisRecordingsWork() {
        val first = record("one", "one-id")
        val second = record("two", "two-id")
        listOf(first, second).forEach { ProcessStateFile.write(it, ProcessState(id = "one-id", stage = ProcessStage.ASR, job = "old-server-job")) }
        assertEquals(first, WorkScheduler.findFolder(root, "one-id"))
        assertNull(ProcessStateFile.own(second))
        val busy = busyRecordings(root, null, { true })
        assertTrue(busy.containsAll(listOf("one-id", first.path)))
        assertFalse(second.path in busy || "two-id" in busy)
    }

    @Test fun trashFailurePreservesRecordAndCleanupMovesOnlyAudio() = runBlocking {
        val dir = processed("Работа/Дейли", "id")
        val transcript = File(dir, "transcript.txt").readBytes()
        val meta = File(dir, Meta.FILE).readBytes()
        val wav = File(dir, "mic.wav").apply { writeText("wav") }
        val archive = File(dir, "old.zip").apply { writeText("archive") }
        var reject = true
        val moved = mutableListOf<String>()
        val store = FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), { null }, { false }, Dispatchers.Unconfined,
            trash = { file -> if (reject) throw IOException("Нет разрешения") else { moved += file.name; assertTrue(file.renameTo(File(tmp.root, "removed-${file.name}"))) } },
            textsOnServer = { { true } })
        assertFalse(store.delete(dir))
        assertEquals("Нет разрешения", store.failure.value)
        store.refresh()
        assertNull(store.failure.value)
        assertTrue(dir.isDirectory)
        reject = false
        assertTrue(store.clearAudio(dir))
        assertEquals(listOf("mic.wav"), moved)
        assertFalse(wav.exists())
        assertArrayEquals(transcript, File(dir, "transcript.txt").readBytes())
        assertArrayEquals(meta, File(dir, Meta.FILE).readBytes())
        assertTrue(archive.exists())
        val detail = store.detail(dir)!!
        assertFalse(detail.cleanable)
        assertFalse(detail.processable)
        assertTrue(detail.summarizable)
    }

    @Test fun withoutTrashARecordingAndItsSoundAreDeletedForeverOnlyAfterConsent() = runBlocking {
        val dir = record("Работа/Дейли", "id")
        File(dir, "mic.wav").writeText("wav")
        val call = processed("Звонок", "call")
        File(call, "mic.wav").writeText("wav")
        val store = FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), { null }, { false }, Dispatchers.Unconfined,
            trash = { error("unexpected trash") }, hasTrash = { false }, textsOnServer = { { true } })
        store.refresh()
        assertFalse(store.trashAvailable.value)

        assertFalse(store.delete(dir))
        assertEquals("Корзина недоступна", store.failure.value)
        assertTrue(File(dir, "mic.wav").isFile)
        assertTrue(store.delete(dir, forever = true))
        assertFalse(dir.exists())
        assertTrue(File(root, "Работа").isDirectory)

        assertFalse(store.clearAudio(call))
        assertTrue(File(call, "mic.wav").isFile)
        assertTrue(store.clearAudio(call, forever = true))
        assertFalse(File(call, "mic.wav").exists())
        assertTrue(File(call, "transcript.txt").isFile)
        assertEquals(listOf("call"), store.items.value.map { it.id })
    }

    @Test fun busyAndUnavailableNativeTrashNeverInvokeDeletion() = runBlocking {
        val dir = record("busy", "id")
        var calls = 0
        val store = FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), { null }, { false }, Dispatchers.Unconfined,
            trash = { calls++ }, busy = { setOf("id") })
        assertFalse(store.delete(dir))
        assertFalse(store.delete(dir, forever = true))
        assertEquals(0, calls)
        assertTrue(dir.exists())
    }
}
