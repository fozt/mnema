package dev.mnema.app.sync

import dev.mnema.app.i18n.Ui
import dev.mnema.app.i18n.UiLang
import dev.mnema.app.processing.ProcessStateFile
import dev.mnema.app.server.Connection
import dev.mnema.app.server.RunnerClient
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.attribute.PosixFilePermission
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import mockwebserver3.SocketEffect
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class LibrarySyncTest {
    @get:Rule val temp = TemporaryFolder()
    private val server = MockWebServer()
    private val library = TestLibrary()
    private val http = RunnerClient.defaultHttp()
    private lateinit var root: File
    private lateinit var checkpoint: File
    @Volatile private var selected: Connection? = null
    private val busy = mutableSetOf<String>()
    private var busyCalls = 0
    private val trashed = mutableListOf<File>()
    private var bin = true
    private var trash: (File) -> Unit = { file ->
        val target = File(temp.root, "trash-${UUID.randomUUID()}")
        Files.move(file.toPath(), target.toPath()); trashed += target
    }

    @Before fun start() {
        root = temp.newFolder("client")
        checkpoint = File(temp.newFolder("state"), "sync.json")
        server.dispatcher = library
        server.start()
        selected = Connection(server.url("/").toString().trimEnd('/'), "private-token")
    }
    @After fun stop() { server.close(); http.dispatcher.executorService.shutdown(); http.connectionPool.evictAll() }
    private val moves = mutableListOf<Pair<String, String>>() // folder names of every sync move
    private var move: (File, File) -> Unit = { a, b -> moves += a.name to b.name; Files.move(a.toPath(), b.toPath()) }
    private fun folders(dir: File = root) = dir.list()!!.filterNot(::hidden).sorted()
    /** The case change [from] → [to] took two moves through a temporary name and no direct one. */
    private fun changedCaseInTwoSteps(from: String, to: String) {
        assertEquals(listOf(from to "$RENAMING$to", "$RENAMING$to" to to), moves.filter { (a, b) -> a == from || b == to })
        assertTrue(moves.none { (a, b) -> a != b && a.equals(b, ignoreCase = true) })
    }
    private fun engine() = LibrarySync(root, checkpoint, { selected }, { busyCalls++; busy.toSet() }, { trash(it) }, { LibraryClient(it, http) }, { bin }, { a, b -> move(a, b) })
    private fun run(): SyncStatus = runBlocking { engine().runOnce() }.also { library.failure?.let { throw AssertionError(it) } }
    private fun ok(): SyncStatus = run().also { assertEquals(emptyList<String>(), it.errors) }
    private fun synced(name: String = "record"): Pair<File, String> {
        val folder = recording(root, name); ok(); return folder to metaId(folder)
    }
    private fun remote(id: String) = library.records.getValue(id)

    @Test fun firstPublicationKeepsIdentityAndNeverUploadsAudioOrProcessState() {
        val folder = recording(root, "record")
        File(folder, "mic.wav").writeText("audio")
        File(folder, ProcessStateFile.FILE).writeText("local process")
        val before = File(folder, FileName.META.fileName).readBytes()
        ok()
        assertArrayEquals(before, File(folder, FileName.META.fileName).readBytes())
        assertEquals(FileName.entries.toSet(), remote(metaId(folder)).files.keys)
        assertEquals("local process", File(folder, ProcessStateFile.FILE).readText())
        assertFalse("private-token" in checkpoint.readText())
        assertFalse(ok().changed)
    }

    @Test fun explicitSyncSelectionCreatesOnlyNewLibrary() {
        root.deleteRecursively()
        initializeLibrary(root, checkpoint)
        assertTrue(root.isDirectory)
        root.deleteRecursively()
        checkpoint.writeText("broken")
        initializeLibrary(root, checkpoint)
        assertFalse(root.exists())
        checkpoint.delete()
        Files.createSymbolicLink(checkpoint.toPath(), File(temp.root, "missing").toPath())
        initializeLibrary(root, checkpoint)
        assertFalse(root.exists())
    }

    @Test fun twoClientsDownloadSharedFixtureBytesAndExactCheckpointThenDoNoWrites() {
        val fixture = fixture()
        val create = wire.decodeFromJsonElement(CreateRecord.serializer(), fixture.getValue("create"))
        val expectedRecord = wire.decodeFromJsonElement(Manifest.serializer(), fixture.getValue("manifest")).records.single()
        library.records[create.id] = Remote(create.path, create.files.toMutableMap(), expectedRecord.tag)
        library.sections += "Работа"
        ok()
        val otherRoot = temp.newFolder("other")
        val otherState = File(temp.root, "other.json")
        val other = LibrarySync(otherRoot, otherState, { selected }, { emptySet() }, { fail("unexpected trash") }, { LibraryClient(it, http) })
        assertTrue(runBlocking { other.runOnce() }.errors.isEmpty())
        val expected = wire.decodeFromString(Checkpoint.serializer(), resource("sync-checkpoint-v1.json")).copy(server = selected!!.url)
        assertEquals(expected, loadCheckpoint(checkpoint))
        assertEquals(expected, loadCheckpoint(otherState))
        library.requests.clear()
        repeat(2) { assertFalse(ok().changed); assertFalse(runBlocking { other.runOnce() }.changed) }
        assertTrue(library.requests.all { it.method == "GET" })
        for (client in listOf(root, otherRoot)) for ((name, body) in create.files) {
            val actual = readFile(File(client, "${create.path}/${name.fileName}"))
            assertArrayEquals(body.text.toByteArray(), actual.bytes)
            assertEquals(Stamp(expectedRecord.files.getValue(name).sha256, body.mtimeNs), actual.stamp)
        }
    }

    @Test fun renameConflictNamesTheRecording() {
        val (folder, id) = synced("2026-10-20_Обзор состояния проекта")
        assertTrue(folder.renameTo(File(root, "2026-10-20_Обзор проекта")))
        remote(id).path = "2026-10-20_Проект"
        val error = run().errors.single()
        assertTrue(error, "Обзор проекта" in error && "2026-10-20" !in error) // the title as the list shows it
    }

    @Test fun summaryMdOfAnEarlierVersionBecomesSummaryTxtWithoutChanges() {
        val (folder, id) = synced()
        val (gone, _) = synced("gone")
        Files.move(File(folder, "summary.txt").toPath(), File(folder, "summary.md").toPath())
        checkpoint.writeText(checkpoint.readText().replace("\"summary.txt\"", "\"summary.md\""))
        assertTrue(gone.deleteRecursively()) // the earlier sync is still known: a deletion, not a new download
        library.requests.clear()
        ok()
        assertEquals("пересказ", File(folder, "summary.txt").readText())
        assertEquals(100L, Files.getLastModifiedTime(File(folder, "summary.txt").toPath()).to(TimeUnit.NANOSECONDS))
        assertFalse(File(folder, "summary.md").exists())
        assertEquals(listOf("DELETE"), library.requests.map { it.method }.filter { it != "GET" })
        assertFalse(gone.exists()); assertEquals(setOf(id), library.records.keys)
    }

    @Test fun independentFilesAndConcurrentChangesUseOriginalNanoseconds() {
        val (folder, id) = synced()
        edit(File(folder, "transcript.txt"), "local", 300)
        remote(id).edit(FileName.SUMMARY, "remote", 400)
        ok()
        assertEquals("local", remote(id).files.getValue(FileName.TRANSCRIPT).text)
        assertEquals("remote", File(folder, "summary.txt").readText())
        edit(File(folder, "transcript.txt"), "older", 500)
        remote(id).edit(FileName.TRANSCRIPT, "newer", 600)
        ok()
        assertEquals("newer", File(folder, "transcript.txt").readText())
        assertEquals(600L, stamp(File(folder, "transcript.txt"))!!.mtimeNs)
        edit(File(folder, "transcript.txt"), "latest local", 800)
        remote(id).edit(FileName.TRANSCRIPT, "older remote", 700)
        ok()
        assertEquals(FileBody("latest local", 800), remote(id).files[FileName.TRANSCRIPT])
    }

    @Test fun equalTimestampConflictAndConcurrentRenamesNeverReplaceEitherSide() {
        val (folder, id) = synced()
        edit(File(folder, "summary.txt"), "local", 300)
        remote(id).edit(FileName.SUMMARY, "remote", 300)
        assertTrue(run().errors.isNotEmpty())
        assertEquals("local", File(folder, "summary.txt").readText())
        assertEquals("remote", remote(id).files.getValue(FileName.SUMMARY).text)
        Files.move(folder.toPath(), File(root, "Local").toPath())
        remote(id).path = "Remote"
        assertTrue(run().errors.isNotEmpty())
        assertTrue(File(root, "Local").exists())
        assertEquals("Remote", remote(id).path)
    }

    @Test fun recordingLeftUnderATemporaryRenameIsRestoredAndNotDeletedEverywhere() {
        val (folder, id) = synced("Работа/Дейли")
        Files.move(folder.toPath(), File(root, "Работа/${RENAMING}дейли").toPath())

        ok()

        assertTrue(File(root, "Работа/дейли/${FileName.META.fileName}").isFile)
        assertTrue(id in library.records)
        // the case changed here, so it goes to the server
        assertEquals("Работа/дейли", remote(id).path)
        assertTrue(trashed.isEmpty())
    }

    @Test fun recordingThatCannotLeaveATemporaryRenameIsNotDeletedAnywhere() {
        val (folder, id) = synced("Работа/Дейли")
        val section = File(root, "Работа")
        Files.move(folder.toPath(), File(section, "${RENAMING}дейли").toPath())
        val rights = Files.getPosixFilePermissions(section.toPath())
        Files.setPosixFilePermissions(section.toPath(), rights - setOf(PosixFilePermission.OWNER_WRITE))
        try {
            val result = run()
            assertTrue(result.errors.isNotEmpty())
        } finally {
            Files.setPosixFilePermissions(section.toPath(), rights)
        }
        assertTrue(id in library.records)
        assertTrue(id in loadCheckpoint(checkpoint).records)
        assertEquals(0, library.requests.count { it.method == "DELETE" })
        assertTrue(trashed.isEmpty())
    }

    @Test fun copiedFolderGetsNewIdWithoutChangingTrackedOriginalOrProcessState() {
        val (original, id) = synced("z-original")
        File(original, ProcessStateFile.FILE).writeText("""{"id":"stable-process"}""")
        original.copyRecursively(File(root, "a-copy"))
        ok()
        assertEquals(id, metaId(original))
        assertNotEquals(id, metaId(File(root, "a-copy")))
        assertEquals(2, library.records.size)
        assertEquals("""{"id":"stable-process"}""", File(root, "a-copy/${ProcessStateFile.FILE}").readText())
    }

    @Test fun nameCollisionsPreserveBothRecordsAndConverge() {
        library.add("occupied", "server")
        recording(root, "occupied")
        ok()
        assertEquals(setOf("server", "оригинал"), root.listFiles()!!.map { File(it, "transcript.txt").readText() }.toSet())
        assertEquals(2, library.records.size)
        assertFalse(ok().changed)
    }

    @Test fun singleMissingIsDeletedAndReturnFromTrashIsPublishedAgain() {
        val (folder, id) = synced()
        val outside = File(temp.root, "finder-trash")
        Files.move(folder.toPath(), outside.toPath())
        ok()
        assertTrue(library.records.isEmpty())
        assertFalse(id in loadCheckpoint(checkpoint).records)
        Files.move(outside.toPath(), folder.toPath())
        ok() // both the server and the checkpoint forgot the id: this is a new recording
        assertEquals(id, metaId(folder))
        assertEquals(setOf(id), library.records.keys)
        assertEquals("оригинал", remote(id).files.getValue(FileName.TRANSCRIPT).text)
    }

    @Test fun twoMissingAreHeldUntilExplicitDeleteAndChangedChoiceIsRejected() {
        val (a, aid) = synced("a"); val (b, bid) = synced("b")
        a.deleteRecursively(); b.deleteRecursively()
        assertEquals(setOf(aid, bid), ok().bulkMissing.toSet())
        assertEquals(2, library.records.size)
        assertTrue(runBlocking { engine().resolveBulk(listOf(aid), false) }.errors.isNotEmpty())
        assertEquals(2, library.records.size)
        assertTrue(runBlocking { engine().resolveBulk(listOf(aid, bid), false) }.errors.isEmpty())
        assertTrue(library.records.isEmpty())
        assertTrue(loadCheckpoint(checkpoint).records.isEmpty())
    }

    @Test fun twoRecordsGoneOnServerKeepLocalCopiesUntilDeleteHereIsChosen() {
        val (a, aid) = synced("a"); val (b, bid) = synced("Работа/b"); val (c, cid) = synced("c")
        library.records.remove(aid); library.records.remove(bid)
        val pending = ok()
        assertEquals(listOf(aid, bid).sorted(), pending.serverMissing)
        assertTrue(pending.bulkMissing.isEmpty())
        assertTrue(a.isDirectory && b.isDirectory); assertTrue(trashed.isEmpty())
        assertEquals(setOf(aid, bid, cid), loadCheckpoint(checkpoint).records.keys)
        assertTrue(runBlocking { engine().resolveBulk(listOf(aid), false) }.errors.isNotEmpty())
        assertTrue(a.isDirectory)
        assertTrue(runBlocking { engine().resolveBulk(listOf(aid, bid), false) }.errors.isEmpty())
        assertFalse(a.exists() || b.exists()); assertTrue(c.isDirectory)
        assertEquals(2, trashed.size)
        assertEquals(setOf(cid), loadCheckpoint(checkpoint).records.keys)
        assertTrue(ok().serverMissing.isEmpty())
    }

    @Test fun lossesOnBothSidesAreAskedOneAfterAnotherStartingHere() {
        val (a, aid) = synced("a"); val (b, bid) = synced("b"); val (_, cid) = synced("c"); val (_, did) = synced("d")
        a.deleteRecursively(); b.deleteRecursively()
        library.records.remove(cid); library.records.remove(did)
        val first = ok()
        assertEquals(listOf(aid, bid).sorted(), first.bulkMissing); assertTrue(first.serverMissing.isEmpty())
        assertTrue(runBlocking { engine().resolveBulk(listOf(cid, did), false) }.errors.isNotEmpty())
        val second = runBlocking { engine().resolveBulk(listOf(aid, bid), true) }
        assertEquals(emptyList<String>(), second.errors)
        assertEquals(listOf(cid, did).sorted(), second.serverMissing)
        assertTrue(File(root, "a").isDirectory && File(root, "b").isDirectory)
        assertTrue(File(root, "c").isDirectory && File(root, "d").isDirectory); assertTrue(trashed.isEmpty())
    }

    @Test fun twoRecordsGoneOnServerAreSentBackWhenRestoreIsChosen() {
        val (a, aid) = synced("a"); val (b, bid) = synced("Работа/b")
        library.records.clear(); library.sections.clear()
        assertEquals(listOf(aid, bid).sorted(), ok().serverMissing)
        assertTrue(runBlocking { engine().resolveBulk(listOf(aid, bid), true) }.errors.isEmpty())
        assertTrue(a.isDirectory && b.isDirectory); assertTrue(trashed.isEmpty())
        assertEquals(mapOf(aid to "a", bid to "Работа/b"), library.records.mapValues { it.value.path })
        assertEquals("оригинал", remote(bid).files.getValue(FileName.TRANSCRIPT).text)
        assertTrue(ok().serverMissing.isEmpty())

        // without a trash even one loss is held
        bin = false
        val (c, cid) = synced("c")
        library.records.remove(cid)
        assertEquals(listOf(cid), ok().serverMissing)
        assertTrue(runBlocking { engine().resolveBulk(listOf(cid), true) }.errors.isEmpty())
        assertTrue(c.isDirectory)
        assertEquals("c", remote(cid).path)
        assertTrue(ok().serverMissing.isEmpty())
    }

    @Test fun restoreRemovesAllChosenCheckpointIdsBeforeAnyDownloadAndSurvivesInterruption() {
        val (a, aid) = synced("a"); val (b, bid) = synced("b")
        root.deleteRecursively()
        assertEquals(2, ok().bulkMissing.size)
        var downloads = 0
        library.after = { req, response ->
            if (req.method == "GET" && req.target.endsWith(FileName.META.fileName)) {
                downloads++
                if (downloads == 1) assertTrue(loadCheckpoint(checkpoint).records.isEmpty())
                if (downloads == 2) dropped() else response
            } else response
        }
        assertTrue(runBlocking { engine().resolveBulk(listOf(aid, bid), true) }.errors.isNotEmpty())
        assertEquals(2, library.records.size)
        library.after = { _, response -> response }
        ok()
        assertTrue(a.exists() && b.exists())
        assertEquals(2, library.records.size)
        assertTrue(library.requests.none { it.method == "DELETE" })
    }

    @Test fun cancellationDuringRestoreLeavesSecondRecordRecoverableOnRestart() = runBlocking<Unit> {
        val (a, aid) = synced("a"); val (b, bid) = synced("b")
        a.deleteRecursively(); b.deleteRecursively()
        var downloads = 0
        lateinit var task: Job
        library.after = { req, response ->
            if (req.method == "GET" && req.target.endsWith(FileName.META.fileName) && ++downloads == 2) task.cancel()
            response
        }
        task = launch { engine().resolveBulk(listOf(aid, bid), true) }
        task.join()
        assertTrue(task.isCancelled)
        library.after = { _, response -> response }
        ok()
        assertTrue(a.exists() && b.exists())
        assertEquals(2, library.records.size)
    }

    @Test fun checkpointWriteFailureStopsRestoreBeforeDownloadsAndPreservesPreviousJson() {
        val (a, aid) = synced("a"); val (b, bid) = synced("b")
        a.deleteRecursively(); b.deleteRecursively()
        val before = checkpoint.readBytes()
        val saved = File(temp.root, "saved-state")
        library.requests.clear()
        library.after = { req, response ->
            if (req.target == "/api/library") {
                Files.move(checkpoint.parentFile!!.toPath(), saved.toPath())
                checkpoint.parentFile!!.writeText("disk unavailable")
            }
            response
        }
        assertTrue(runBlocking { engine().resolveBulk(listOf(aid, bid), true) }.errors.isNotEmpty())
        assertArrayEquals(before, File(saved, checkpoint.name).readBytes())
        assertTrue(library.requests.none { "/files/" in it.target })
    }

    @Test fun remoteDeleteUsesInjectedTrashForEntireFolderAndDefersBusyOrTrashFailure() {
        val (folder, id) = synced()
        File(folder, "mic.wav").writeText("audio")
        library.records.remove(id)
        busy += id
        ok(); assertTrue(folder.exists()); assertTrue(id in loadCheckpoint(checkpoint).records)
        busy.clear()
        val workingTrash = trash
        trash = { throw IOException("native unavailable") }
        assertTrue(run().errors.isNotEmpty())
        assertTrue(folder.exists()); assertTrue(id in loadCheckpoint(checkpoint).records)
        trash = workingTrash
        ok()
        assertFalse(folder.exists())
        assertEquals("audio", File(trashed.single(), "mic.wav").readText())
    }

    @Test fun withoutTrashEvenOneServerDeletionWaitsAndIsDeletedForeverOnlyByChoice() {
        val (folder, id) = synced("a"); val (other, _) = synced("b")
        File(folder, "mic.wav").writeText("audio")
        bin = false
        library.records.remove(id)
        val pending = ok()
        assertEquals(listOf(id), pending.serverMissing)
        assertTrue(pending.withoutTrash)
        assertTrue(folder.isDirectory)
        // choosing the trash without one deletes nothing: the question repeats
        val again = runBlocking { engine().resolveBulk(listOf(id), false) }
        assertEquals(listOf(id), again.serverMissing)
        assertTrue(File(folder, "mic.wav").isFile)
        assertTrue(runBlocking { engine().resolveBulk(listOf(id), false, forever = true) }.errors.isEmpty())
        assertFalse(folder.exists()); assertTrue(other.isDirectory); assertTrue(trashed.isEmpty())
        assertFalse(id in loadCheckpoint(checkpoint).records)
        assertTrue(ok().serverMissing.isEmpty())
    }

    @Test fun aHeldServerDeletionGoesToTheTrashOnceItIsBack() {
        val (folder, id) = synced("a")
        bin = false
        library.records.remove(id)
        assertEquals(listOf(id), ok().serverMissing)
        bin = true
        assertTrue(ok().serverMissing.isEmpty())
        assertFalse(folder.exists())
        assertEquals(1, trashed.size)
    }

    @Test fun missingBusyPathRetainsCheckpointAndKnownRootDeletionIsSafe() {
        val (folder, id) = synced()
        folder.deleteRecursively(); busy += id
        ok(); assertTrue(id in loadCheckpoint(checkpoint).records); assertTrue(id in library.records)
        busy.clear(); root.deleteRecursively()
        ok(); assertTrue(library.records.isEmpty()); assertFalse(root.exists())
    }

    @Test fun initialMissingRootDoesNotCreateOrDeleteAnythingAndLostCheckpointKeepsAudio() {
        root.deleteRecursively(); library.add("server")
        assertTrue(run().errors.isNotEmpty())
        assertFalse(root.exists()); assertFalse(checkpoint.exists()); assertEquals(1, library.records.size)
        root.mkdir(); ok()
        val folder = File(root, "server")
        File(folder, "mic.wav").writeText("local audio")
        checkpoint.delete(); ok()
        assertEquals("local audio", File(folder, "mic.wav").readText())
        assertTrue(trashed.isEmpty())
    }

    @Test fun serverSwitchDuringManifestOrFileResponseNeverAppliesOldResponse() {
        library.add("remote")
        val initial = selected
        library.after = { _, response -> selected = Connection("http://other", "other-token"); response }
        assertTrue(run().errors.isNotEmpty()); assertTrue(root.list()!!.isEmpty()); assertFalse(checkpoint.exists())
        selected = initial; library.after = { _, response -> response }; ok()
        val record = library.records.entries.single()
        record.value.edit(FileName.TRANSCRIPT, "old server reply", 300)
        library.after = { req, response -> if (req.target.endsWith("/transcript.txt")) selected = Connection("http://other", "other-token"); response }
        val before = loadCheckpoint(checkpoint).records[record.key]!!.files
        assertTrue(run().errors.isNotEmpty())
        assertEquals("server", File(root, "remote/transcript.txt").readText())
        assertEquals(before, loadCheckpoint(checkpoint).records[record.key]!!.files)
    }

    @Test fun newServerDoesNotInheritOldDeletion() {
        val (folder, id) = synced()
        folder.deleteRecursively()
        val other = MockWebServer()
        val otherLibrary = TestLibrary()
        otherLibrary.add("new library", id = id)
        other.dispatcher = otherLibrary; other.start()
        try {
            selected = Connection(other.url("/").toString().trimEnd('/'), "other-token")
            ok()
            assertTrue(File(root, "new library").exists())
            assertEquals(setOf(id), otherLibrary.records.keys)
            assertEquals(selected!!.url, loadCheckpoint(checkpoint).server)
        } finally { other.close() }
    }

    @Test fun localEditOrProcessingStartedDuringDownloadDefersApply() {
        val (folder, id) = synced()
        remote(id).edit(FileName.TRANSCRIPT, "remote", 200)
        library.after = { req, response ->
            if (req.target.endsWith("/transcript.txt")) edit(File(folder, "transcript.txt"), "typing", 300)
            response
        }
        assertTrue(run().errors.isNotEmpty()); assertEquals("typing", File(folder, "transcript.txt").readText())
        library.after = { _, response -> response }; ok()
        assertEquals("typing", remote(id).files.getValue(FileName.TRANSCRIPT).text)
        remote(id).edit(FileName.TRANSCRIPT, "remote later", 400)
        library.after = { req, response -> if (req.target.endsWith("/transcript.txt")) busy += id; response }
        assertTrue(run().errors.isNotEmpty()); assertEquals("typing", File(folder, "transcript.txt").readText())
        library.after = { _, response -> response }; busy.clear(); ok()
        assertEquals("remote later", File(folder, "transcript.txt").readText())
    }

    @Test fun remoteEditAfterManifestReturns409AndNextScanDoesNotOverwriteNewerBytes() {
        val (folder, id) = synced()
        edit(File(folder, "transcript.txt"), "local", 200)
        var fired = false
        library.after = { req, response ->
            if (req.target == "/api/library" && !fired) { fired = true; remote(id).edit(FileName.TRANSCRIPT, "new remote", 300) }
            response
        }
        assertTrue(run().errors.isNotEmpty())
        assertEquals("new remote", remote(id).files.getValue(FileName.TRANSCRIPT).text)
        library.after = { _, response -> response }; ok()
        assertEquals("new remote", File(folder, "transcript.txt").readText())
    }

    @Test fun lostPostReplyKeepsSavedUuidAndNextManifestCompletesWithoutSecondPost() {
        val folder = recording(root, "record")
        library.after = { req, response -> if (req.method == "POST") dropped() else response }
        assertTrue(run().errors.isNotEmpty())
        val id = metaId(folder)
        assertEquals(setOf(id), library.records.keys)
        library.after = { _, response -> response }; ok()
        assertEquals(id, metaId(folder)); assertEquals(1, library.requests.count { it.method == "POST" })
        assertFalse(ok().changed)
    }

    @Test fun lostPutDeleteAndPatchRepliesAreObservedWithoutReplayingStaleOperations() {
        val (folder, id) = synced()
        edit(File(folder, "transcript.txt"), "local", 200)
        library.requests.clear()
        library.after = { req, response -> if (req.method == "PUT" && "/files/" in req.target) dropped() else response }
        assertTrue(run().errors.isNotEmpty())
        library.after = { _, response -> response }; ok()
        assertEquals(1, library.requests.count { it.method == "PUT" })
        File(folder, "summary.txt").delete()
        library.after = { req, response -> if (req.method == "DELETE") dropped() else response }
        assertTrue(run().errors.isNotEmpty())
        library.after = { _, response -> response }; ok()
        assertEquals(1, library.requests.count { it.method == "DELETE" })
        assertFalse(FileName.SUMMARY in remote(id).files)
        Files.move(folder.toPath(), File(root, "Renamed").toPath())
        library.after = { req, response -> if (req.method == "PATCH") dropped() else response }
        assertTrue(run().errors.isNotEmpty())
        library.after = { _, response -> response }; ok()
        assertEquals(1, library.requests.count { it.method == "PATCH" })
        assertEquals("Renamed", loadCheckpoint(checkpoint).records.getValue(id).path)
        File(root, "Renamed").deleteRecursively()
        library.requests.clear()
        library.after = { req, response -> if (req.method == "DELETE") dropped() else response }
        assertTrue(run().errors.isNotEmpty()); assertTrue(id in loadCheckpoint(checkpoint).records)
        library.after = { _, response -> response }; ok()
        assertFalse(id in loadCheckpoint(checkpoint).records)
        assertEquals(1, library.requests.count { it.method == "DELETE" })
    }

    @Test fun offlineAnd503RetainLocalEditsDeletionAndPreviousCheckpoint() {
        val (a, _) = synced("a"); val (b, bid) = synced("b")
        a.deleteRecursively(); edit(File(b, "summary.txt"), "offline edit", 300)
        val before = checkpoint.readBytes()
        library.before = { response(503) }
        assertTrue(run().errors.isNotEmpty()); assertArrayEquals(before, checkpoint.readBytes())
        assertFalse(a.exists()); assertEquals("offline edit", File(b, "summary.txt").readText())
        library.before = { dropped() }
        assertTrue(run().errors.isNotEmpty()); assertArrayEquals(before, checkpoint.readBytes())
        library.before = { null }; ok()
        assertEquals(setOf(bid), library.records.keys)
        assertEquals("offline edit", remote(bid).files.getValue(FileName.SUMMARY).text)
    }

    @Test fun incompleteWireManifestNeverTrashesOrChangesCheckpointButExplicitEmptyWorks() {
        val (folder, id) = synced()
        val before = checkpoint.readBytes()
        val files = folder.listFiles()!!.associate { it.name to it.readText() }
        for (body in listOf("{}", "{\"records\":[]}", "{\"sections\":[]}")) {
            library.requests.clear()
            library.before = { json(body) }
            assertTrue(run().errors.isNotEmpty())
            assertTrue(trashed.isEmpty())
            assertEquals(files, folder.listFiles()!!.associate { it.name to it.readText() })
            assertArrayEquals(before, checkpoint.readBytes())
            assertTrue(library.requests.all { it.method == "GET" })
        }
        library.before = { json("{\"records\":[],\"sections\":[]}") }
        ok()
        assertFalse(folder.exists())
        assertEquals(1, trashed.size)
        assertFalse(id in loadCheckpoint(checkpoint).records)
    }

    @Test fun malformedSectionInFirstManifestNeverTrashesKnownRecords() {
        val (folder, _) = synced()
        val before = checkpoint.readBytes()
        for (section in listOf("../bad", "a/b", "", "/bad", ".obsidian")) {
            library.requests.clear()
            library.before = { json(wire.encodeToString(Manifest.serializer(), Manifest(sections = listOf(section)))) }
            assertTrue(run().errors.isNotEmpty())
            assertTrue(folder.exists()); assertTrue(trashed.isEmpty())
            assertArrayEquals(before, checkpoint.readBytes())
            assertTrue(library.requests.all { it.method == "GET" })
        }
    }

    @Test fun secondManifestIsFullyValidatedBeforeAnySectionMutation() {
        val (folder, _) = synced()
        File(root, "A").mkdir(); ok()
        File(root, "A").delete()
        val before = checkpoint.readBytes()
        var manifests = 0
        library.requests.clear()
        library.after = { req, response ->
            if (req.target == "/api/library" && ++manifests == 2) {
                json(wire.encodeToString(Manifest.serializer(), Manifest(
                    records = library.records.map { (id, remote) -> remote.record(id) },
                    sections = listOf("A", "zz/../bad"),
                )))
            } else response
        }
        assertTrue(run().errors.isNotEmpty())
        assertTrue(folder.exists()); assertTrue(trashed.isEmpty())
        assertArrayEquals(before, checkpoint.readBytes())
        assertEquals(setOf("A"), library.sections)
        assertTrue(library.requests.all { it.method == "GET" })
    }

    @Test fun tokenChangeDuringFileDownloadStopsApplyEvenWhenAddressIsUnchanged() {
        val (folder, id) = synced()
        val before = loadCheckpoint(checkpoint).records.getValue(id).files
        remote(id).edit(FileName.TRANSCRIPT, "old token reply", 300)
        library.after = { req, response ->
            if (req.target.endsWith("/transcript.txt")) selected = selected!!.copy(token = "new-token")
            response
        }
        assertTrue(run().errors.isNotEmpty())
        assertEquals("оригинал", File(folder, "transcript.txt").readText())
        assertEquals(before, loadCheckpoint(checkpoint).records.getValue(id).files)
    }

    @Test fun malformedRemoteRecordOrPathCannotCauseAnyLocalDeletions() {
        val (folder, _) = synced()
        val before = checkpoint.readBytes()
        val id = UUID.randomUUID().toString()
        val stamp = "{\"sha256\":\"s\",\"mtime_ns\":100}"
        val badRecords = listOf(
            "{\"id\":\"$id\",\"path\":\"../outside\",\"files\":{\".mnema-meta.json\":$stamp},\"tag\":\"opaque\"}",
            "{\"id\":\"$id\",\"path\":\"remote\",\"files\":{\"summary.txt\":$stamp},\"tag\":\"opaque\"}",
        )
        for (record in badRecords) {
            library.before = { json("{\"records\":[$record],\"sections\":[]}") }
            assertTrue(run().errors.isNotEmpty())
            assertTrue(folder.exists()); assertTrue(trashed.isEmpty())
            assertArrayEquals(before, checkpoint.readBytes())
        }
    }

    @Test fun partialDownloadAfterMetaNeverBecomesAnInferredDelete() {
        val id = library.add("remote")
        library.before = { req -> if (req.target.endsWith("/summary.txt")) response(503) else null }
        assertTrue(run().errors.isNotEmpty())
        assertTrue(File(root, "remote/${FileName.META.fileName}").exists())
        assertTrue(id in loadCheckpoint(checkpoint).records)
        library.before = { null }; ok()
        assertEquals("remote summary", File(root, "remote/summary.txt").readText())
        assertTrue(id in library.records)
        assertTrue(library.requests.none { it.method == "DELETE" })
    }

    @Test fun sectionRenameRecordMoveAndRemoteNonemptyRacePreserveFiles() {
        File(root, "Empty").mkdir(); ok()
        Files.move(File(root, "Empty").toPath(), File(root, "New").toPath()); ok()
        assertEquals(setOf("New"), library.sections)
        val (folder, id) = synced()
        Files.move(folder.toPath(), File(root, "New/record").toPath()); ok()
        assertEquals("New/record", remote(id).path)
        File(root, "Empty").mkdir(); ok(); File(root, "Empty").delete()
        var manifests = 0
        library.after = { req, response -> if (req.target == "/api/library" && ++manifests == 2) library.add("Empty/arrived"); response }
        assertTrue(run().errors.isNotEmpty())
        assertTrue(library.records.values.any { it.path == "Empty/arrived" })
        library.after = { _, response -> response }; ok()
        assertTrue(File(root, "Empty/arrived/transcript.txt").exists())
    }

    @Test fun strayFilesInSectionNeverTurnItIntoRecordOrDeleteItsRecords() {
        val (folder, id) = synced("Работа/Встреча")
        val section = File(root, "Работа")
        File(section, "memo.m4a").writeText("audio")
        File(section, "mic.wav").writeBytes(ByteArray(32044))
        File(section, "transcript.txt").writeText("случайный файл")
        File(section, "summary.txt").writeText("случайный пересказ")
        ok()
        assertEquals(listOf(id to "Работа/Встреча"), library.records.map { (key, remote) -> key to remote.path })
        assertEquals(setOf("Работа"), library.sections)
        assertTrue(folder.isDirectory); assertTrue(trashed.isEmpty())
        assertFalse(File(section, FileName.META.fileName).exists())
        assertTrue(id in loadCheckpoint(checkpoint).records)
    }

    @Test fun recordFilesInSyncedEmptySectionKeepItASection() {
        val section = File(root, "Работа").apply { mkdir() }
        ok()
        assertEquals(listOf("Работа"), loadCheckpoint(checkpoint).sections)
        File(section, "transcript.txt").writeText("случайный файл")
        File(section, "mic.wav").writeBytes(ByteArray(32044))
        ok()
        assertFalse(File(section, FileName.META.fileName).exists())
        assertTrue(library.records.isEmpty()); assertEquals(setOf("Работа"), library.sections)
        File(section, "transcript.txt").delete()
        val (folder, id) = synced("Встреча")
        Files.move(folder.toPath(), File(section, "Встреча").toPath())
        ok()
        assertEquals(listOf(id to "Работа/Встреча"), library.records.map { (key, remote) -> key to remote.path })
        assertFalse(File(section, FileName.META.fileName).exists()); assertTrue(trashed.isEmpty())
    }

    @Test fun knownSectionStaysASectionAfterServerSwitch() {
        val section = File(root, "Работа").apply { mkdir() }
        ok()
        File(section, FileName.SUMMARY.fileName).writeText("случайный файл")
        val other = MockWebServer()
        val otherLibrary = TestLibrary()
        other.dispatcher = otherLibrary; other.start()
        try {
            selected = Connection(other.url("/").toString().trimEnd('/'), "other-token")
            ok()
            assertFalse(File(section, FileName.META.fileName).exists())
            assertTrue(otherLibrary.records.isEmpty()); assertEquals(setOf("Работа"), otherLibrary.sections)
        } finally { other.close() }
    }

    @Test fun recordInsideRecordIsOneMessageWithBothTitlesAndDeletesNothing() {
        val (outer, _) = synced("2026-10-20_Обзор состояния проекта")
        val (inner, _) = synced("2026-10-21_ИИ встреча")
        Files.move(inner.toPath(), File(outer, inner.name).toPath())
        val error = run().errors.single()
        assertTrue(error, "«ИИ встреча»" in error && "«Обзор состояния проекта»" in error && "2026-10-2" !in error)
        assertEquals(2, library.records.size); assertTrue(trashed.isEmpty())
    }

    @Test fun otherUnreadableFoldersKeepTheGeneralMessage() {
        val (outer, _) = synced("Звонок")
        val (inner, _) = synced("Внутри")
        val (broken, _) = synced("Сломанная")
        Files.move(inner.toPath(), File(outer, "Внутри").toPath())
        File(broken, FileName.META.fileName).writeText("broken")
        val errors = run().errors
        assertTrue(errors.toString(), "Не все локальные записи удалось прочитать; удаления отложены" in errors)
        assertEquals(2, errors.size); assertTrue(trashed.isEmpty())
    }

    @Test fun syncErrorsAreEnglishInAnEnglishInterface() {
        val (outer, _) = synced("Звонок")
        val (inner, _) = synced("Внутри")
        Files.move(inner.toPath(), File(outer, "Внутри").toPath())
        val before = Ui.lang
        Ui.lang = UiLang.EN
        try {
            val errors = run().errors
            assertTrue(errors.toString(), errors.single().startsWith("“Внутри” is inside the recording “Звонок”"))
            errors.forEach { assertFalse(it, it.replace("Внутри", "").replace("Звонок", "").any { c -> c in 'а'..'я' || c in 'А'..'Я' }) }
        } finally { Ui.lang = before }
    }

    @Test fun knownRecordWithoutItsFilesIsNeverDeleted() {
        val (folder, id) = synced("Работа/Дейли")
        val (other, otherId) = synced("Работа/Встреча")
        folder.listFiles()!!.forEach { it.delete() }
        File(folder, "notes.pdf").writeText("x")
        val deeper = File(other, "Старое").apply { mkdir() }
        other.listFiles()!!.filter { it != deeper }.forEach { Files.move(it.toPath(), File(deeper, it.name).toPath()) }
        val errors = run().errors
        assertTrue("В папке записи «Работа/Дейли» нет её файлов" in errors)
        assertTrue("В папке записи «Работа/Встреча» нет её файлов" in errors)
        assertEquals(setOf(id, otherId), library.records.keys)
        assertEquals(setOf(id, otherId), loadCheckpoint(checkpoint).records.keys)
        assertTrue(trashed.isEmpty())
        folder.deleteRecursively(); other.deleteRecursively()
        File(root, "Работа/Материалы/Старое").apply { mkdirs() }.resolve("doc.pdf").writeText("x")
        assertEquals(setOf(id, otherId), ok().bulkMissing.toSet()) // a foreign folder does not hold deletions
    }

    @Test fun recordMovedIntoANewSubfolderDeletesNothingAnywhere() {
        val (folder, id) = synced("Работа/Встреча")
        val phone = temp.newFolder("phone")
        val other = LibrarySync(phone, File(temp.root, "phone.json"), { selected }, { emptySet() }, { fail("unexpected trash") }, { LibraryClient(it, http) })
        assertTrue(runBlocking { other.runOnce() }.errors.isEmpty())
        Files.move(folder.toPath(), File(root, "Работа/Старые").apply { mkdir() }.resolve("Встреча").toPath())
        val before = checkpoint.readBytes()
        library.requests.clear()
        val result = run()
        val error = result.errors.single()
        assertTrue(error, "«Встреча»" in error && "«Работа»" in error)
        assertFalse(result.changed); assertTrue(result.bulkMissing.isEmpty()); assertTrue(trashed.isEmpty())
        assertArrayEquals(before, checkpoint.readBytes())
        assertTrue(library.requests.all { it.method == "GET" })
        assertTrue(runBlocking { other.runOnce() }.errors.isEmpty())
        assertEquals(setOf(id), library.records.keys)
        assertEquals("оригинал", File(phone, "Работа/Встреча/transcript.txt").readText())
        Files.move(File(root, "Работа/Старые").toPath(), File(root, "Старые").toPath()) // the subfolder became a section
        ok()
        assertTrue(runBlocking { other.runOnce() }.errors.isEmpty())
        assertEquals("Старые/Встреча", remote(id).path); assertTrue(trashed.isEmpty())
        assertEquals("оригинал", File(phone, "Старые/Встреча/transcript.txt").readText())
    }

    /**
     * Another device moved a recording into a section, then renamed the section only by case ("projects" → "Projects"): the section
     * here gets the same name through two renames via a temporary name, and nothing goes back to the server.
     */
    @Test fun serverMoveIntoSectionAndCaseOnlySectionRenameReachThisDevice() {
        val name = "2026-08-12_Дейли таблицы и графики"
        val (_, id) = synced(name)
        fun shown() = dev.mnema.app.recordings.libraryFolders(root, loadCheckpoint(checkpoint).sections.toSet()).let { layout ->
            layout.sections to layout.records.map { it.relativeTo(root.canonicalFile).invariantSeparatorsPath }
        }
        remote(id).apply { path = "projects/$name"; fixtureTag = null }
        library.sections += "projects"
        ok()
        assertEquals(listOf("projects"), folders())
        assertEquals(listOf("projects") to listOf("projects/$name"), shown())

        remote(id).apply { path = "Projects/$name"; fixtureTag = null }
        library.sections -= "projects"; library.sections += "Projects"
        library.requests.clear(); moves.clear()
        assertTrue(ok().changed)
        assertEquals(listOf("Projects"), folders())
        assertEquals(listOf(name), folders(File(root, "Projects")))
        changedCaseInTwoSteps("projects", "Projects")
        assertEquals(listOf("Projects") to listOf("Projects/$name"), shown())
        assertEquals("оригинал", File(root, "Projects/$name/transcript.txt").readText())
        assertEquals("пересказ", File(root, "Projects/$name/summary.txt").readText())
        assertEquals(id, metaId(File(root, "Projects/$name")))
        assertEquals(listOf("Projects"), loadCheckpoint(checkpoint).sections)
        assertEquals("Projects/$name", loadCheckpoint(checkpoint).records.getValue(id).path)
        // the server's case is not sent back: no recording move, no section deletion
        assertTrue(library.requests.all { it.method == "GET" })
        assertFalse(ok().changed)
        assertTrue(library.requests.all { it.method == "GET" })
        assertEquals("Projects/$name", remote(id).path)
        assertEquals(setOf("Projects"), library.sections)
        assertEquals(1, library.records.size)
        assertTrue(trashed.isEmpty())

        val fresh = recording(root, "Projects/Новая")
        ok()
        assertEquals("Projects/Новая", remote(metaId(fresh)).path)
        assertEquals(setOf("Projects"), library.sections)

        // an ordinary move still works afterwards
        remote(id).apply { path = "Работа/$name"; fixtureTag = null }
        library.sections += "Работа"
        ok()
        assertEquals("оригинал", File(root, "Работа/$name/transcript.txt").readText())
        assertEquals(listOf("Новая"), folders(File(root, "Projects")))
        assertTrue(trashed.isEmpty())
    }

    /** A section renamed only by case by hand here (the Files app): recordings and the section go to the server in the new spelling. */
    @Test fun caseOnlySectionRenameHereReachesTheServer() {
        val (_, id) = synced("projects/Дейли")
        val (_, other) = synced("projects/Планёрка")
        // like the Files app: a direct rename(2); Files.move does not move to the same name in another case
        check(File(root, "projects").renameTo(File(root, "Projects")))
        library.requests.clear(); moves.clear()

        ok()

        assertEquals("Projects/Дейли", remote(id).path)
        assertEquals("Projects/Планёрка", remote(other).path)
        assertEquals(setOf("Projects"), library.sections)
        assertEquals(listOf("Projects"), folders())
        assertEquals(listOf("Дейли", "Планёрка"), folders(File(root, "Projects")))
        assertTrue(moves.isEmpty())
        assertEquals(listOf("Projects"), loadCheckpoint(checkpoint).sections)
        assertEquals(setOf("Projects/Дейли", "Projects/Планёрка"), loadCheckpoint(checkpoint).records.values.map { it.path }.toSet())
        assertEquals(2, library.records.size)
        assertTrue(library.requests.none { it.method == "POST" && it.url.pathSegments.size == 2 }) // no second copy
        assertTrue(trashed.isEmpty())
        library.requests.clear()
        assertFalse(ok().changed)
        assertTrue(library.requests.all { it.method == "GET" })
    }

    /** A recording renamed only by case here: the new spelling goes to the server. */
    @Test fun caseOnlyRecordRenameHereReachesTheServer() {
        val (folder, id) = synced("Работа/дейли")
        check(folder.renameTo(File(root, "Работа/Дейли")))
        ok()
        assertEquals("Работа/Дейли", remote(id).path)
        assertEquals(listOf("Дейли"), folders(File(root, "Работа")))
        assertEquals("Работа/Дейли", loadCheckpoint(checkpoint).records.getValue(id).path)
        library.requests.clear()
        assertFalse(ok().changed)
        assertTrue(library.requests.all { it.method == "GET" })
    }

    /** A recording renamed only by case on the server: the folder here changes it through two renames. */
    @Test fun caseOnlyRecordRenameOnServerRenamesTheFolderHere() {
        val (_, id) = synced("Работа/дейли")
        remote(id).apply { path = "Работа/Дейли"; fixtureTag = null }
        library.requests.clear(); moves.clear()
        ok()
        assertEquals(listOf("Дейли"), folders(File(root, "Работа")))
        changedCaseInTwoSteps("дейли", "Дейли")
        assertEquals("оригинал", File(root, "Работа/Дейли/transcript.txt").readText())
        assertEquals(id, metaId(File(root, "Работа/Дейли")))
        assertEquals("Работа/Дейли", loadCheckpoint(checkpoint).records.getValue(id).path)
        assertFalse(ok().changed)
        assertTrue(library.requests.all { it.method == "GET" })
        assertEquals("Работа/Дейли", remote(id).path)
        remote(id).apply { path = "Работа/Планёрка"; fixtureTag = null }
        ok()
        assertEquals(listOf("Планёрка"), folders(File(root, "Работа")))
    }

    @Test fun emptySectionRenamedOnServerOnlyByCaseFollowsHere() {
        File(root, "projects").mkdir(); ok()
        library.sections -= "projects"; library.sections += "Projects"
        library.requests.clear(); moves.clear()
        ok()
        assertEquals(listOf("Projects"), folders())
        changedCaseInTwoSteps("projects", "Projects")
        assertFalse(ok().changed)
        assertTrue(library.requests.all { it.method == "GET" })
        assertEquals(setOf("Projects"), library.sections)
        assertEquals(listOf("Projects"), loadCheckpoint(checkpoint).sections)
    }

    /** A busy recording in the section: the server's case waits, nothing is deleted or sent back; later it goes through. */
    @Test fun busyRecordDefersTheServerCaseRenameOfItsSection() {
        val (_, id) = synced("projects/Дейли")
        val (_, other) = synced("projects/Планёрка")
        for (key in listOf(id, other)) remote(key).apply { path = path.replaceFirst("projects/", "Projects/"); fixtureTag = null }
        library.sections -= "projects"; library.sections += "Projects"
        busy += id
        library.requests.clear()

        run()

        assertEquals(listOf("projects"), folders())
        assertTrue(library.requests.all { it.method == "GET" })
        assertEquals(setOf("Projects"), library.sections)
        assertEquals(listOf("projects"), loadCheckpoint(checkpoint).sections)
        assertEquals(2, library.records.size); assertTrue(trashed.isEmpty())

        busy.clear(); moves.clear()
        ok()
        assertEquals(listOf("Projects"), folders())
        changedCaseInTwoSteps("projects", "Projects")
        assertEquals(listOf("Дейли", "Планёрка"), folders(File(root, "Projects")))
        assertEquals(listOf("Projects"), loadCheckpoint(checkpoint).sections)
        assertEquals(setOf("Projects/Дейли", "Projects/Планёрка"), loadCheckpoint(checkpoint).records.values.map { it.path }.toSet())
        assertEquals(setOf("Projects/Дейли", "Projects/Планёрка"), library.records.values.map { it.path }.toSet())
        assertTrue(library.requests.all { it.method == "GET" })
    }

    /** Android refused the section case change: the old name stays without an error or PATCH, and the next sync finishes it. */
    @Test fun refusedCaseRenameKeepsTheOldNameAndALaterSyncFinishesIt() {
        val (_, id) = synced("projects/Дейли")
        remote(id).apply { path = "Projects/Дейли"; fixtureTag = null }
        library.sections -= "projects"; library.sections += "Projects"
        move = { a, b -> throw java.nio.file.FileSystemException(a.path, b.path, "Operation not permitted") }
        library.requests.clear()

        repeat(2) { ok() }

        assertEquals(listOf("projects"), folders())
        assertEquals(listOf("Дейли"), folders(File(root, "projects")))
        assertTrue(library.requests.all { it.method == "GET" })
        assertEquals("Projects/Дейли", remote(id).path)
        assertEquals(setOf("Projects"), library.sections)

        move = { a, b -> moves += a.name to b.name; Files.move(a.toPath(), b.toPath()) }
        ok()
        assertEquals(listOf("Projects"), folders())
        changedCaseInTwoSteps("projects", "Projects")
        assertEquals(id, metaId(File(root, "Projects/Дейли")))
        assertEquals(listOf("Projects"), loadCheckpoint(checkpoint).sections)
        assertTrue(library.requests.all { it.method == "GET" })
    }

    /**
     * A recording was renamed on another device, but Android refused to rename its folder here (EPERM from MediaProvider):
     * the recording name goes back to the server, and the next sync runs without an error.
     */
    @Test fun refusedLocalRenameKeepsTheFolderNameEverywhereInsteadOfFailingEverySync() {
        val name = "projects/2026-10-27_16-23"
        val (folder, id) = synced(name)
        remote(id).apply { path = "projects/2026-10-27_Короткая проверка связи"; fixtureTag = null }
        remote(id).edit(FileName.SUMMARY, "пересказ с Mac", 900)
        move = { a, b -> throw java.nio.file.FileSystemException(a.path, b.path, "Operation not permitted") }

        val errors = run().errors
        assertEquals(listOf("Android не дал переименовать папку «$name»; запись осталась под прежним именем"), errors)
        assertTrue(folder.isDirectory)
        assertEquals(name, remote(id).path)
        assertEquals("пересказ с Mac", File(folder, "summary.txt").readText())

        library.requests.clear()
        assertFalse(ok().changed)
        assertTrue(library.requests.all { it.method == "GET" })
        assertEquals(1, library.records.size)
    }

    /** The server keeps the section in another case, and a recording busy here is not downloaded as a second copy. */
    @Test fun busyRecordIsNotDownloadedAgainWhenTheServerSpellsItsSectionDifferently() {
        val (_, id) = synced("projects/Дейли")
        remote(id).apply { path = "Projects/Дейли"; fixtureTag = null }
        library.sections -= "projects"; library.sections += "Projects"
        checkpoint.delete() // a new server or a recording with no trace in the last sync
        busy += "projects/Дейли"

        run()

        assertEquals(1, library.records.size)
        assertEquals("Projects/Дейли", remote(id).path)
        assertEquals(listOf("projects"), folders())
        assertEquals(listOf("Дейли"), folders(File(root, "projects")))
    }

    /** The last sync remembers the path in another case: a recording without its files is still the same recording, its id is kept. */
    @Test fun knownRecordInAnotherCaseWithoutItsFilesIsNeverDeleted() {
        val (folder, id) = synced("projects/Дейли")
        val state = loadCheckpoint(checkpoint)
        saveCheckpoint(checkpoint, state.copy(
            records = state.records.mapValues { (_, record) -> record.copy(path = "Projects/Дейли") },
            sections = listOf("Projects"),
        ))
        folder.listFiles()!!.forEach { it.delete() }
        File(folder, "notes.pdf").writeText("x")

        assertTrue("В папке записи «projects/Дейли» нет её файлов" in run().errors)
        assertEquals(setOf(id), library.records.keys)
        assertTrue(trashed.isEmpty())
    }

    @Test fun sectionLeftUnderATemporaryRenameIsRestoredAndItsRecordIsNotDeleted() {
        val (_, id) = synced("Работа/Дейли")
        Files.move(File(root, "Работа").toPath(), File(root, "${RENAMING}Работа").toPath())

        ok()

        assertTrue(File(root, "Работа/Дейли/${FileName.META.fileName}").isFile)
        assertEquals("оригинал", File(root, "Работа/Дейли/transcript.txt").readText())
        assertEquals("Работа/Дейли", remote(id).path)
        assertEquals(setOf("Работа"), library.sections)
        assertTrue(trashed.isEmpty())
    }

    @Test fun hiddenFoldersAreNotSectionsAndDoNotHoldDeletion() {
        val (a, aid) = synced("a")
        for (path in listOf(".obsidian/plugins/x/main.js", ".git/objects/ab/cd", ".Trash-1000/files/old/transcript.txt", ".mnema-sync-1/summary.txt")) {
            File(root, path).apply { parentFile!!.mkdirs() }.writeText("x")
        }
        File(root, ".stfolder").mkdir()
        a.deleteRecursively()
        val result = ok()
        assertTrue(result.bulkMissing.isEmpty())
        assertTrue(library.records.isEmpty()); assertTrue(library.sections.isEmpty())
        assertFalse(aid in loadCheckpoint(checkpoint).records)
        assertTrue(File(root, ".Trash-1000/files/old/transcript.txt").exists())
    }

    @Test fun serverSwitchAndBackKeepsIdsWithoutDuplicates() {
        val ids = (0..2).map { synced("rec $it").second }.toSet()
        val first = selected
        val other = MockWebServer()
        val otherLibrary = TestLibrary()
        other.dispatcher = otherLibrary; other.start()
        try {
            selected = Connection(other.url("/").toString().trimEnd('/'), "other-token")
            ok()
            selected = first
            ok()
            assertFalse(ok().changed)
            assertEquals(ids, library.records.keys)
            assertEquals(ids, otherLibrary.records.keys)
            assertEquals(listOf("rec 0", "rec 1", "rec 2"), root.list()!!.sorted())
            otherLibrary.failure?.let { throw AssertionError(it) }
        } finally { other.close() }
    }

    @Test fun twoDevicesMovingToNewServerKeepOneCopy() {
        val (_, id) = synced("call")
        val otherRoot = temp.newFolder("phone")
        val other = LibrarySync(otherRoot, File(temp.root, "phone.json"), { selected }, { emptySet() }, { fail("unexpected trash") }, { LibraryClient(it, http) })
        assertTrue(runBlocking { other.runOnce() }.errors.isEmpty())
        val next = MockWebServer()
        val nextLibrary = TestLibrary()
        next.dispatcher = nextLibrary; next.start()
        try {
            selected = Connection(next.url("/").toString().trimEnd('/'), "next-token")
            ok()
            assertTrue(runBlocking { other.runOnce() }.errors.isEmpty())
            assertEquals(setOf(id), nextLibrary.records.keys)
            assertEquals(listOf("call"), root.list()!!.toList())
            assertEquals(listOf("call"), otherRoot.list()!!.toList())
            nextLibrary.failure?.let { throw AssertionError(it) }
        } finally { next.close() }
    }

    @Test fun duplicateIdWithoutTrackedPathKeepsEarlierCreatedCopy() {
        val (folder, id) = synced("original")
        folder.copyRecursively(File(root, "b"))
        Thread.sleep(20) // the second copy's ctime is surely later
        folder.copyRecursively(File(root, "a"))
        folder.deleteRecursively()
        ok()
        assertEquals(id, metaId(File(root, "b")))
        assertNotEquals(id, metaId(File(root, "a")))
        assertEquals("b", remote(id).path)
        assertEquals(2, library.records.size)
    }

    @Test fun idleSyncDoesNotRewriteCheckpoint() {
        repeat(3) { synced("rec $it") }
        File(root, "Пустой").mkdir()
        ok()
        val old = java.nio.file.attribute.FileTime.fromMillis(1_000_000)
        Files.setLastModifiedTime(checkpoint.toPath(), old)
        repeat(2) { assertFalse(ok().changed) }
        assertEquals(old, Files.getLastModifiedTime(checkpoint.toPath()))
        edit(File(root, "rec 1/summary.txt"), "новый пересказ", 500)
        assertTrue(ok().changed)
        assertNotEquals(old, Files.getLastModifiedTime(checkpoint.toPath()))
    }

    @Test fun busyCheckCostDoesNotGrowWithLibrarySize() {
        repeat(2) { synced("small $it") }
        busyCalls = 0; assertFalse(ok().changed)
        val small = busyCalls
        repeat(6) { synced("large $it") }
        busyCalls = 0; assertFalse(ok().changed)
        assertEquals(small, busyCalls)
    }

    @Test fun systemTrashRefusalIsShownInWordsAndOtherRecordsStillSync() {
        val (folder, id) = synced("a")
        library.records.remove(id)
        library.add("z-later", id = "ffffffff-ffff-ffff-ffff-ffffffffffff")
        val system = NativeTrash(37, { true }, { File(temp.root, "probe") }) { throw IllegalStateException("trash directory cannot be created") }
        trash = system::move
        assertEquals(listOf("«a»: Система не переместила запись в корзину"), run().errors)
        assertTrue(folder.exists()); assertTrue(id in loadCheckpoint(checkpoint).records)
        assertEquals("server", File(root, "z-later/transcript.txt").readText())
    }

    /** Deletions, copies and busy recordings: the same decisions as the desktop (shared fixture sync-decisions-v1). */
    @Test fun sharedSyncDecisionsMatchTheDesktopEngine() {
        val cases = Json.parseToJsonElement(resource("sync-decisions-v1.json")).jsonObject.getValue("cases").jsonArray.map { it.jsonObject }
        for ((i, case) in cases.withIndex()) {
            val name = case.getValue("name").jsonPrimitive.content
            fun strings(key: String) = case[key]?.jsonArray.orEmpty().map { it.jsonPrimitive.content }
            fun pairs(key: String) = case[key]?.jsonObject.orEmpty().mapValues { it.value.jsonPrimitive.content }
            root = temp.newFolder("decisions-$i")
            checkpoint = File(temp.newFolder("decisions-state-$i"), "sync.json")
            library.records.clear(); library.sections.clear(); busy.clear(); library.before = { null }
            val ids = strings("synced").associateWith { synced(it).second }
            val names = ids.entries.associate { (path, id) -> id to path }
            pairs("copied").forEach { (copy, source) -> File(root, source).copyRecursively(File(root, copy)) }
            pairs("renamed").forEach { (from, to) -> File(root, to).parentFile!!.mkdirs(); assertTrue(File(root, from).renameTo(File(root, to))) }
            strings("removed").forEach { assertTrue(File(root, it).deleteRecursively()) }
            strings("broken").forEach { File(root, "$it/${FileName.META.fileName}").writeText("broken") }
            if (case["root_removed"]?.jsonPrimitive?.boolean == true) assertTrue(root.deleteRecursively())
            strings("lost").forEach { library.records.remove(ids.getValue(it)) }
            pairs("server_renamed").forEach { (from, to) -> remote(ids.getValue(from)).path = to }
            if (case["checkpoint_lost"]?.jsonPrimitive?.boolean == true) assertTrue(checkpoint.delete())
            strings("busy").forEach { busy += ids.getValue(it) }
            if (case["server_unavailable"]?.jsonPrimitive?.boolean == true) library.before = { response(503) }
            val unreadable = case["root_unreadable"]?.jsonPrimitive?.boolean == true
            val rights = if (unreadable) Files.getPosixFilePermissions(root.toPath()) else null
            if (unreadable) Files.setPosixFilePermissions(root.toPath(), emptySet())
            val moved = mutableListOf<String>()
            trash = { file ->
                moved += file.relativeTo(root).invariantSeparatorsPath
                Files.move(file.toPath(), File(temp.root, "trash-${UUID.randomUUID()}").toPath())
            }

            val status = try { run() } finally { rights?.let { Files.setPosixFilePermissions(root.toPath(), it) } }

            assertEquals(name, case["errors"]?.jsonPrimitive?.boolean ?: false, status.errors.isNotEmpty())
            val held = (status.bulkMissing + status.serverMissing).map { names.getValue(it) }.sorted()
            assertEquals(name, strings("held"), held)
            if (held.isNotEmpty()) assertEquals(name, case.getValue("held_on").jsonPrimitive.content == "server", status.bulkMissing.isEmpty())
            val local = root.walkTopDown().filter { it.name == FileName.META.fileName }.map { it.parentFile!!.relativeTo(root).invariantSeparatorsPath }
            assertEquals(name, strings("local").sorted(), local.sorted().toList())
            assertEquals(name, pairs("server"), library.records.entries.associate { (id, remote) -> remote.path to (names[id] ?: "new") })
            assertEquals(name, strings("trashed").sorted(), moved.sorted())
        }
    }
}

internal class Remote(var path: String, val files: MutableMap<FileName, FileBody>, var fixtureTag: String? = null) {
    fun record(id: String): Record {
        val stamps = files.mapValues { (_, body) -> Stamp(sha256(body.text.toByteArray()), body.mtimeNs) }
        return Record(id, path, stamps, fixtureTag ?: sha256((path + stamps.toString()).toByteArray()))
    }
    fun edit(name: FileName, text: String, ns: Long) { files[name] = FileBody(text, ns); fixtureTag = null }
}

/** The server side checks HTTP preconditions independently of the client's three-way algorithm. */
internal class TestLibrary : Dispatcher() {
    val records = linkedMapOf<String, Remote>()
    val sections = linkedSetOf<String>()
    val requests = mutableListOf<RecordedRequest>()
    var before: (RecordedRequest) -> MockResponse? = { null }
    var after: (RecordedRequest, MockResponse) -> MockResponse = { _, response -> response }
    var failure: Throwable? = null
    fun add(path: String, text: String = "server", id: String = UUID.randomUUID().toString()): String {
        records[id] = Remote(path, mutableMapOf(
            FileName.META to FileBody("""{"id":"$id","started":"2020-01-02T03:04:05+03:00","device":"Other"}""", 200),
            FileName.TRANSCRIPT to FileBody(text, 200), FileName.SUMMARY to FileBody("remote summary", 200),
        ))
        path.substringBeforeLast('/', "").takeIf { it.isNotEmpty() }?.let { sections += it }
        return id
    }
    override fun dispatch(request: RecordedRequest): MockResponse = try {
        requests += request
        before(request) ?: after(request, handle(request))
    } catch (e: Throwable) { failure = e; response(500) }
    private fun handle(req: RecordedRequest): MockResponse {
        val url = req.url
        val parts = url.pathSegments.drop(2)
        if (parts.isEmpty()) return json(wire.encodeToString(Manifest.serializer(), Manifest(records.map { (id, record) -> record.record(id) }, sections.sorted())))
        if (parts[0] == "sections") {
            val path = url.queryParameter("path")!!
            if (req.method == "DELETE") {
                if (records.values.any { it.path.startsWith("$path/") }) return response(409)
                sections -= path
            } else sections += path
            return response(204)
        }
        if (parts.size == 1 && req.method == "POST") {
            val create = wire.decodeFromString(CreateRecord.serializer(), req.body!!.utf8())
            if (create.id in records) return response(409)
            var path = create.path; var n = 2
            while (records.values.any { it.path == path }) path = "${create.path} (${n++})"
            records[create.id] = Remote(path, create.files.toMutableMap())
            path.substringBeforeLast('/', "").takeIf { it.isNotEmpty() }?.let { sections += it }
            return json(wire.encodeToString(Record.serializer(), records.getValue(create.id).record(create.id)))
        }
        val id = parts[1]
        val remote = records[id] ?: return response(if (req.method == "DELETE" && parts.size == 2) 204 else 404)
        if (parts.size == 2) {
            if (req.method == "DELETE") {
                if (req.headers["If-Match"] != remote.record(id).tag) return response(409)
                records.remove(id); return response(204)
            }
            val move = wire.decodeFromString(MoveRecord.serializer(), req.body!!.utf8())
            if (remote.path != move.expectedPath || records.any { it.key != id && it.value.path == move.path }) return response(409)
            remote.path = move.path; remote.fixtureTag = null
            move.path.substringBeforeLast('/', "").takeIf { it.isNotEmpty() }?.let { sections += it }
            return json(wire.encodeToString(Record.serializer(), remote.record(id)))
        }
        val name = FileName.entries.single { it.fileName == parts[3] }
        val file = remote.files[name]
        val stamp = remote.record(id).files[name]
        if (req.method == "GET") {
            if (file == null) return response(404)
            return MockResponse.Builder().body(file.text).addHeader("ETag", "\"${stamp!!.sha256}\"")
                .addHeader("X-Mnema-Mtime-Ns", file.mtimeNs.toString()).build()
        }
        if (req.headers["If-Match"] != stamp?.sha256 || (stamp == null && req.headers["If-None-Match"] != "*")) return response(409)
        if (req.method == "DELETE") { remote.files.remove(name); remote.fixtureTag = null; return response(204) }
        remote.edit(name, req.body!!.utf8(), req.headers["X-Mnema-Mtime-Ns"]!!.toLong())
        return json(wire.encodeToString(Stamp.serializer(), remote.record(id).files.getValue(name)))
    }
}
internal fun response(code: Int) = MockResponse.Builder().code(code).build()
internal fun json(body: String) = MockResponse.Builder().body(body).addHeader("Content-Type", "application/json").build()
internal fun dropped() = MockResponse.Builder().onResponseStart(SocketEffect.CloseSocket()).build()
