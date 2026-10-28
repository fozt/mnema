package dev.mnema.app.processing

import dev.mnema.app.server.RecordingResult
import dev.mnema.app.server.RefusedResult
import dev.mnema.app.sync.resource
import dev.mnema.app.transcript.Meta
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.attribute.PosixFilePermission
import java.util.TimeZone
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class ApplyTest {
    @get:Rule val tmp = TemporaryFolder()
    private val name = "2026-10-22_16-01"
    private val target = "2026-10-22_Дейли"
    private fun recording() = tmp.newFolder(name).also {
        Meta.ensureRecording(it, "recording-id") { Meta.isoNow(1_790_000_000_000, TimeZone.getTimeZone("Europe/Moscow")) }
    }
    private fun result(folder: String? = target) = RecordingResult(
        files = linkedMapOf("transcript.txt" to "[0:12] mic: x\n", "summary.md" to null), folder = folder,
        run = buildJsonObject { put("kind", "process"); put("settings", buildJsonObject { put("node", "Сервер") }) },
    )
    private fun apply(dir: File, result: RecordingResult = result(), recordingNow: String? = null) =
        applyResult(dir, result, "job-1", "node.example", name, recordingNow, summaryOnly = false)

    private fun withoutRun(dir: File) {
        val before = requireNotNull(Meta.read(dir))
        File(dir, Meta.FILE).writeText(JsonObject(before + ("runs" to JsonArray(emptyList()))).toString())
    }

    private fun texts(dir: File): Map<String, String> =
        listOf("transcript.txt", "summary.md", "translation.md").filter { File(dir, it).isFile }.associateWith { File(dir, it).readText() }

    private fun strings(value: JsonObject): Map<String, String> =
        value.filterValues { it != JsonNull }.mapValues { it.value.jsonPrimitive.content }

    @Test fun resultIsAppliedToTheFolderAsInTheSharedContract() {
        val cases = Json.parseToJsonElement(resource("recording-api-v1.json")).jsonObject.getValue("apply").jsonArray
        for (case in cases.map { it.jsonObject }) {
            val label = case.getValue("case").jsonPrimitive.content
            val root = tmp.newFolder()
            case["taken"]?.jsonArray?.forEach { File(root, it.jsonPrimitive.content).mkdir() }
            val dir = File(root, case.getValue("folder").jsonPrimitive.content).apply { mkdir() }
            Meta.ensureRecording(dir, "recording-id") { Meta.isoNow(1_790_000_000_000, TimeZone.getTimeZone("Europe/Moscow")) }
            val before = strings(case.getValue("files").jsonObject)
            before.forEach { (file, text) -> File(dir, file).writeText(text) }
            val result = Json.decodeFromJsonElement(RecordingResult.serializer(), case.getValue("result"))
            val sent = case["sent"]?.jsonPrimitive?.content ?: dir.name
            val summaryOnly = case.getValue("op").jsonPrimitive.content == "recording_summary"
            // a refusal is a processing outcome, PipelineTest checks it
            if (case["refused"]?.jsonPrimitive?.boolean == true) continue
            val applied = applyResult(dir, result, "j1", null, sent, null, summaryOnly)
            val after = case.getValue("after").jsonObject
            assertEquals(label, after.getValue("folder").jsonPrimitive.content, applied.name)
            assertEquals(label, strings(after.getValue("files").jsonObject), texts(applied))
            assertTrue(label, Meta.hasJob(applied, "j1"))
        }
    }

    @Test fun appliesFilesRenameAndRunOnceAndKeepsMetadata() {
        val dir = recording()
        File(dir, "summary.md").writeText("старый пересказ")
        val applied = apply(dir)
        assertEquals(target, applied.name)
        assertFalse(dir.exists())
        assertEquals("[0:12] mic: x\n", File(applied, "transcript.txt").readText())
        assertFalse(File(applied, "summary.md").exists())
        val meta = requireNotNull(Meta.read(applied))
        assertEquals(JsonPrimitive("recording-id"), meta["id"])
        val run = requireNotNull(Meta.lastRun(applied))
        assertEquals(JsonPrimitive("job-1"), run["job"])
        assertEquals(JsonPrimitive("node.example"), run["settings"]!!.jsonObject["server"])
        assertEquals(JsonPrimitive("Сервер"), run["settings"]!!.jsonObject["node"])
        File(applied, "transcript.txt").writeText("правка владельца")
        assertEquals(applied, apply(applied))
        assertEquals("правка владельца", File(applied, "transcript.txt").readText())
        assertEquals(1, Meta.read(applied)!!["runs"]!!.jsonArray.size)
    }

    @Test fun retryAfterFilesAndAfterRenameFinishesOneRun() {
        val dir = recording()
        val blocked = File(dir, "summary.md").apply { mkdir() }
        File(blocked, "keep").writeText("занято")
        assertThrows(IOException::class.java) { apply(dir) }
        assertEquals("[0:12] mic: x\n", File(dir, "transcript.txt").readText())
        assertFalse(Meta.hasJob(dir, "job-1"))
        blocked.deleteRecursively()
        val renamed = apply(dir)
        // a snapshot after the rename, before the last atomic history write.
        withoutRun(renamed)
        assertEquals(renamed, apply(renamed))
        assertEquals(target, renamed.name)
        assertEquals(1, Meta.read(renamed)!!["runs"]!!.jsonArray.size)
    }

    @Test fun userRenameDuringApplyKeepsItsName() {
        val dir = recording()
        val applied = apply(dir)
        withoutRun(applied)
        val manual = File(tmp.root, "Ручное имя")
        Files.move(applied.toPath(), manual.toPath())
        assertEquals(manual, apply(manual))
        assertEquals("[0:12] mic: x\n", File(manual, "transcript.txt").readText())
    }

    @Test fun userRenameBeforeTheFirstAttemptIsRespected() {
        val dir = recording()
        val manual = File(tmp.root, "Моё имя папки")
        Files.move(dir.toPath(), manual.toPath())
        assertEquals(manual, apply(manual))
        assertTrue(Meta.hasJob(manual, "job-1"))
    }

    @Test fun refusedRenameAppliesTheResultInTheSameFolderWithARemark() {
        val dir = recording()
        val refused = { _: File, _: File -> throw java.nio.file.FileSystemException("$name -> $target: Operation not permitted") }
        val applied = applyResult(dir, result(), "job-1", "node.example", name, null, summaryOnly = false, move = refused)
        assertEquals(dir, applied)
        assertEquals(name, applied.name)
        assertFalse(File(tmp.root, target).exists())
        assertEquals("[0:12] mic: x\n", File(dir, "transcript.txt").readText())
        val run = requireNotNull(Meta.lastRun(dir))
        assertEquals(JsonPrimitive("job-1"), run["job"])
        assertEquals(
            listOf("Название «Дейли» не применено: Android не дал переименовать папку."),
            run.getValue("remarks").jsonArray.map { it.jsonPrimitive.content },
        )
        val note = run.getValue("notes").jsonArray.single().jsonObject
        assertEquals("title", note.getValue("step").jsonPrimitive.content)
        // a repeat of the same result adds nothing and does not touch the folder
        assertEquals(dir, applyResult(dir, result(), "job-1", "node.example", name, null, summaryOnly = false, move = refused))
        assertEquals(1, Meta.read(dir)!!["runs"]!!.jsonArray.size)
    }

    @Test fun renameRefusedByTheFileSystemAlsoKeepsTheFolderAndTheResult() {
        val dir = recording()
        val rights = Files.getPosixFilePermissions(tmp.root.toPath())
        val applied = try {
            Files.setPosixFilePermissions(tmp.root.toPath(), rights - setOf(
                PosixFilePermission.OWNER_WRITE, PosixFilePermission.GROUP_WRITE, PosixFilePermission.OTHERS_WRITE,
            ))
            apply(dir)
        } finally {
            Files.setPosixFilePermissions(tmp.root.toPath(), rights)
        }
        assertEquals(dir, applied)
        assertTrue(Meta.hasJob(dir, "job-1"))
        assertEquals(1, Meta.lastRun(dir)!!.getValue("notes").jsonArray.size)
        // the next processing with another job tries to name the folder again
        val renamed = applyResult(dir, result(), "job-2", null, name, null, summaryOnly = false)
        assertEquals(target, renamed.name)
    }

    @Test fun metadataFailureIsVisibleAndRetryKeepsTheExistingIdentity() {
        val dir = recording()
        val meta = File(dir, Meta.FILE)
        val original = meta.readText()
        meta.delete()
        meta.mkdir()
        assertThrows(IOException::class.java) { apply(dir) }
        // the history is written last: the folder is already under the new name, the journal goes with it
        val renamed = File(tmp.root, target)
        assertTrue(renamed.isDirectory)
        assertFalse(Meta.hasJob(renamed, "job-1"))
        File(renamed, Meta.FILE).delete()
        File(renamed, Meta.FILE).writeText(original)
        assertEquals(renamed, apply(renamed))
        assertEquals(JsonPrimitive("recording-id"), Meta.read(renamed)!!["id"])
        assertTrue(Meta.hasJob(renamed, "job-1"))
    }

    @Test fun collisionNearTheByteLimitKeepsAValidUnicodeName() {
        val dir = recording()
        val full = "я".repeat(127) + "a"
        tmp.newFolder(full)
        val renamed = apply(dir, result(full))
        assertTrue(renamed.name.endsWith(" (2)"))
        assertTrue(renamed.name.toByteArray(Charsets.UTF_8).size <= 255)
        assertFalse(renamed.name.contains('\uFFFD'))
        assertTrue(renamed.isDirectory)
    }

    @Test fun symlinksCannotWriteOutsideTheRecording() {
        val dir = recording()
        val outside = tmp.newFile("outside").apply { writeText("keep") }
        Files.createSymbolicLink(File(dir, "transcript.txt").toPath(), outside.toPath())
        assertThrows(IOException::class.java) { apply(dir) }
        assertEquals("keep", outside.readText())
        assertFalse(Meta.hasJob(dir, "job-1"))
    }

    @Test fun invalidNamesAndActiveRecordingDoNotRename() {
        val dir = recording()
        for (bad in listOf("a/b", "..", ".", "", "x\\y", "я".repeat(128), "a\u0000b", "a\uD800b")) {
            assertEquals(dir, applyResult(dir, result(bad), "bad-$bad", null, name, null, summaryOnly = false))
        }
        assertEquals(dir, applyResult(dir, result(), "active", null, name, name, summaryOnly = false))
    }

    @Test fun collisionUsesAnotherNameAndUnexpectedFilesAreRefusedBeforeWriting() {
        tmp.newFolder(target)
        val dir = recording()
        val malicious = result().copy(files = linkedMapOf("transcript.txt" to "new", "../outside" to "bad"))
        assertThrows(RefusedResult::class.java) { apply(dir, malicious) }
        assertFalse(File(dir, "transcript.txt").exists())
        assertFalse(File(tmp.root, "outside").exists())
        assertEquals("$target (2)", apply(dir).name)
    }

    @Test fun danglingSymlinkReservesTheDestinationAndJournalCannotEscapeTheFolder() {
        val dir = recording()
        val occupied = File(tmp.root, target)
        Files.createSymbolicLink(occupied.toPath(), File(tmp.root, "missing").toPath())
        val journal = File(dir, ".mnema-apply.json")
        journal.writeText("""{"job":"job-1","from":"$name","target":"../outside"}""")
        assertThrows(IOException::class.java) { apply(dir) }
        assertFalse(File(dir, "transcript.txt").exists())
        journal.delete()
        val applied = apply(dir)
        assertEquals("$target (2)", applied.name)
        assertTrue(Files.isSymbolicLink(occupied.toPath()))
    }

    @Test fun failureBeforeTheFirstFileCanRetryAfterTheJournalWasSaved() {
        val dir = recording()
        val blocker = File(dir, "transcript.txt").apply { mkdir() }
        assertThrows(IOException::class.java) { apply(dir) }
        assertTrue(File(dir, ".mnema-apply.json").isFile)
        assertFalse(Meta.hasJob(dir, "job-1"))
        blocker.delete()
        val applied = apply(dir)
        assertEquals(target, applied.name)
        assertEquals(1, Meta.read(applied)!!["runs"]!!.jsonArray.size)
    }
}
