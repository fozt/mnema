package dev.mnema.app.sync

import dev.mnema.app.processing.ProcessStateFile
import java.io.File
import java.nio.file.Files
import java.nio.file.attribute.FileTime
import java.util.concurrent.TimeUnit
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class SyncFilesTest {
    @get:Rule val temp = TemporaryFolder()

    @Test fun sharedPythonFixturesPreserveActualHashesBytesAndNanoseconds() {
        val fixture = fixture()
        val create = wire.decodeFromJsonElement(CreateRecord.serializer(), fixture.getValue("create"))
        val manifest = wire.decodeFromJsonElement(Manifest.serializer(), fixture.getValue("manifest"))
        val root = temp.newFolder()
        val folder = File(root, create.path).apply { mkdirs() }
        for ((name, body) in create.files) {
            val bytes = body.text.toByteArray()
            assertEquals(manifest.records.single().files.getValue(name).sha256, sha256(bytes))
            writeFile(File(folder, name.fileName), bytes, body.mtimeNs, null)
            assertArrayEquals(bytes, readFile(File(folder, name.fileName)).bytes)
            assertEquals(body.mtimeNs, readFile(File(folder, name.fileName)).stamp.mtimeNs)
        }
        val before = File(folder, FileName.META.fileName).readBytes()
        assertEquals(create.id, ensureIdentity(folder))
        assertArrayEquals(before, File(folder, FileName.META.fileName).readBytes())
        val scan = scan(root, emptySet())
        assertTrue(scan.complete)
        assertEquals(manifest.records.single().copy(tag = ""), scan.manifest.records.single())
        val checkpoint = wire.decodeFromString(Checkpoint.serializer(), resource("sync-checkpoint-v1.json"))
        val state = File(temp.newFolder(), "state/checkpoint.json")
        saveCheckpoint(state, checkpoint)
        assertEquals(checkpoint, loadCheckpoint(state))
        assertEquals(Json.parseToJsonElement(resource("sync-checkpoint-v1.json")), Json.parseToJsonElement(state.readText()))
    }

    @Test fun scanIsCompleteBeyondUiLimitAndDoesNotReadAudioOrProcessIdentity() {
        val root = temp.newFolder()
        repeat(35) { recording(root, "Работа/$it") }
        File(root, "Пустой").mkdir()
        val audio = File(root, "Звук").apply { mkdir() }
        File(audio, "mic.wav").writeText("not a wav")
        val retold = File(root, "Пересказ").apply { mkdir() }
        File(retold, "summary.md").writeText("пересказ")
        val work = """{"id":"process-id","stage":"done"}"""
        File(retold, ProcessStateFile.FILE).writeText(work)
        val result = scan(root, emptySet())
        assertTrue(result.complete)
        assertEquals(36, result.manifest.records.size)
        assertEquals(listOf("Пустой", "Работа"), result.manifest.sections)
        assertEquals(listOf("Звук"), result.blockedPaths)
        assertFalse(File(audio, FileName.META.fileName).exists())
        assertNotEquals("process-id", metaId(retold))
        assertEquals(work, File(retold, ProcessStateFile.FILE).readText())
    }

    @Test fun sharedLayoutFixtureMatchesTheDesktopRule() {
        val cases = Json.parseToJsonElement(resource("library-layout-v1.json")).jsonObject.getValue("cases").jsonArray.map { it.jsonObject }
        for (case in cases) {
            val name = case.getValue("name").jsonPrimitive.content
            fun strings(key: String) = case[key]?.jsonArray.orEmpty().map { it.jsonPrimitive.content }
            val root = temp.newFolder()
            for (path in strings("records")) recording(root, path).also { folder ->
                if ("record_files" in case) { File(folder, "transcript.txt").delete(); File(folder, "summary.md").delete() }
                strings("record_files").forEach { File(folder, it).writeBytes(ByteArray(32044)) }
            }
            for (path in strings("tree")) {
                val target = File(root, path)
                if (path.endsWith("/")) target.mkdirs()
                else target.apply { parentFile!!.mkdirs() }.writeBytes(if (target.extension in setOf("wav", "m4a")) ByteArray(32044) else "x".toByteArray())
            }
            val result = scan(root, emptySet(), sections = strings("known_sections").toSet(), records = strings("known_records").toSet())
            val metas = root.walkTopDown().filter { it.name == FileName.META.fileName }.map { it.parentFile!!.relativeTo(root).invariantSeparatorsPath }
            assertEquals(name, strings("records").sorted(), metas.sorted().toList())
            if (case["complete"]?.jsonPrimitive?.boolean == false) {
                assertFalse(name, result.complete); assertTrue(name, result.blockedPaths.isNotEmpty())
                continue
            }
            assertTrue(name, result.complete)
            assertEquals(name, emptyList<String>(), result.blockedPaths)
            assertEquals(name, strings("records").sorted(), result.manifest.records.map { it.path }.sorted())
            assertEquals(name, strings("sections"), result.manifest.sections)
        }
    }

    @Test fun busyPathsAndIdsAreUntouchedAndInvalidMetaBlocksDeletionInference() {
        val root = temp.newFolder()
        val a = recording(root, "a")
        val b = recording(root, "b")
        val before = File(a, FileName.META.fileName).readBytes()
        val result = scan(root, setOf(metaId(a), b.absolutePath))
        assertTrue(result.complete)
        assertTrue(result.manifest.records.isEmpty())
        assertEquals(setOf("a", "b"), result.blockedPaths.toSet())
        assertArrayEquals(before, File(a, FileName.META.fileName).readBytes())
        File(a, FileName.META.fileName).writeText("broken")
        assertFalse(scan(root, emptySet()).complete)
    }

    @Test fun pathAndFileLinksCannotEscapeRootButInternalLinksAreAllowed() {
        val root = temp.newFolder()
        val outside = recording(temp.newFolder(), "outside")
        Files.createSymbolicLink(File(root, "escape").toPath(), outside.toPath())
        assertFalse(scan(root, emptySet()).complete)
        Files.delete(File(root, "escape").toPath())
        val local = recording(root, "local")
        val original = File(outside, "transcript.txt").readText()
        File(local, "transcript.txt").delete()
        Files.createSymbolicLink(File(local, "transcript.txt").toPath(), File(outside, "transcript.txt").toPath())
        assertFalse(scan(root, emptySet()).complete)
        assertEquals(original, File(outside, "transcript.txt").readText())
        Files.delete(File(local, "transcript.txt").toPath())
        File(local, "internal.txt").writeText("inside")
        Files.createSymbolicLink(File(local, "transcript.txt").toPath(), File(local, "internal.txt").toPath())
        assertTrue(scan(root, emptySet()).complete)
        listOf("../outside", "/abs", "a/../b", "a/b/c", "a//b", ".obsidian", "Работа/.git", ".mnema-sync-1").forEach {
            assertThrows(SyncException::class.java) { safePath(root, it) }
        }
    }

    @Test fun missingCorruptCheckpointIsFullSyncButReadErrorsAreNotSwallowed() {
        val file = File(temp.newFolder(), "sync.json")
        assertEquals(Checkpoint(), loadCheckpoint(file))
        file.writeText("broken")
        assertEquals(Checkpoint(), loadCheckpoint(file))
        file.delete(); file.mkdir()
        assertThrows(java.io.IOException::class.java) { loadCheckpoint(file) }
        val root = File(temp.root, "missing")
        assertTrue(scan(root, emptySet()).missingRoot)
        assertFalse(root.exists())
    }

    @Test fun danglingRootLinkAndNonregularMetaAreIncompleteNotMissing() {
        val root = File(temp.root, "linked")
        Files.createSymbolicLink(root.toPath(), File(temp.root, "gone").toPath())
        val missing = scan(root, emptySet())
        assertFalse(missing.complete)
        assertFalse(missing.missingRoot)
        assertEquals(listOf("."), missing.blockedPaths)
        val real = temp.newFolder()
        val folder = recording(real, "record")
        File(folder, FileName.META.fileName).delete()
        File(folder, FileName.META.fileName).mkdir()
        val unreadable = scan(real, emptySet())
        assertFalse(unreadable.complete)
        assertEquals(listOf("record"), unreadable.blockedPaths)
    }

    @Test fun invalidUtf8OrNumericMetaIdentityAreNotPublished() {
        val root = temp.newFolder()
        val folder = recording(root, "record")
        File(folder, "transcript.txt").writeBytes(byteArrayOf(0xc3.toByte(), 0x28))
        assertFalse(scan(root, emptySet()).complete)
        File(folder, "transcript.txt").writeText("ok")
        File(folder, FileName.META.fileName).writeText("{\"id\":12345678901234567890123456789012}")
        assertFalse(scan(root, emptySet()).complete)
        val state = File(temp.root, "bad-utf8.json")
        state.writeBytes(byteArrayOf(0xc3.toByte(), 0x28))
        assertEquals(Checkpoint(), loadCheckpoint(state))
    }

    @Test fun conditionalAtomicWritePreservesPreviousBytesAndNanoseconds() {
        val file = File(temp.newFolder(), "text")
        writeFile(file, "before".toByteArray(), 100L, null)
        val original = stamp(file)
        writeFile(file, "new".toByteArray(), 200L, original)
        assertThrows(SyncException::class.java) { writeFile(file, "stale".toByteArray(), 300L, original) }
        assertEquals("new", file.readText())
        assertEquals(200L, stamp(file)!!.mtimeNs)
        assertEquals(listOf("text"), file.parentFile!!.list()!!.toList())
    }
}

internal fun resource(name: String): String = checkNotNull(SyncFilesTest::class.java.getResource("/$name")).readText()
internal fun fixture(): JsonObject = Json.parseToJsonElement(resource("library-v1.json")).jsonObject
internal fun recording(root: File, path: String): File {
    val folder = File(root, path).apply { mkdirs() }
    val id = java.util.UUID.randomUUID().toString()
    File(folder, FileName.META.fileName).writeText("""{"id":"$id","started":"2020-01-02T03:04:05+03:00","runs":[{"remarks":["human"]}],"unknown":{"value":1.5}}""")
    edit(File(folder, "transcript.txt"), "оригинал", 100)
    edit(File(folder, "summary.md"), "пересказ", 100)
    return folder
}
internal fun edit(file: File, text: String, ns: Long) {
    file.writeText(text)
    Files.setLastModifiedTime(file.toPath(), FileTime.from(ns, TimeUnit.NANOSECONDS))
}
internal fun metaId(folder: File): String = Json.parseToJsonElement(File(folder, FileName.META.fileName).readText()).jsonObject.getValue("id").jsonPrimitive.content
