package dev.mnema.app.recordings

import dev.mnema.app.i18n.Ui
import dev.mnema.app.i18n.UiLang
import dev.mnema.app.processing.ProcessStage
import dev.mnema.app.processing.ProcessState
import dev.mnema.app.processing.ProcessStateFile
import dev.mnema.app.transcript.Meta
import java.io.File
import java.nio.file.Files
import java.util.TimeZone
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeFalse
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import dev.mnema.app.ui.recordings.sectionChoices
import java.io.IOException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking

class SectionMoveTest {
    @get:Rule val tmp = TemporaryFolder()
    private val root get() = File(tmp.root, "Mnema").apply { mkdirs() }
    private val sound = ByteArray(4_000) { (it % 251).toByte() }

    private fun stopped(name: String, id: String = name) = File(root, name).apply {
        mkdirs()
        File(this, "mic.wav").writeBytes(sound)
        File(this, "system.wav").writeBytes(sound)
        Meta.ensureRecording(this, id) { Meta.isoNow(1_792_612_008_754, TimeZone.getTimeZone("UTC")) }
    }

    private fun section(name: String) = File(root, name).apply { mkdirs() }

    private fun assertUntouched(dir: File) {
        assertTrue(dir.isDirectory)
        assertArrayEquals(sound, File(dir, "mic.wav").readBytes())
        assertArrayEquals(sound, File(dir, "system.wav").readBytes())
    }

    @Test fun stoppedRecordingMovesIntoTheChosenSectionWithItsFilesAndId() {
        val dir = stopped("2026-10-23_10-15", id = "record-id")
        val work = section("Работа")

        val moved = moveToSection(root, dir, "Работа", busy = emptySet())

        assertEquals(File(work.canonicalFile, "2026-10-23_10-15"), moved)
        assertFalse(dir.exists())
        assertUntouched(moved)
        assertEquals("record-id", recordingId(moved))
        assertEquals(listOf(moved), libraryFolders(root).records)
        assertEquals(listOf("Работа"), libraryFolders(root).sections)
    }

    @Test fun takenNameInTheSectionGetsANumberAndNothingIsMerged() {
        section("Работа")
        val earlier = stopped("Работа/2026-10-23_10-15", id = "earlier")
        stopped("Работа/2026-10-23_10-15 (2)", id = "second")
        val dir = stopped("2026-10-23_10-15", id = "new")

        val moved = moveToSection(root, dir, "Работа", busy = emptySet())

        assertEquals("2026-10-23_10-15 (3)", moved.name)
        assertEquals("new", recordingId(moved))
        assertEquals("earlier", recordingId(earlier))
        assertUntouched(moved)
    }

    @Test fun vanishedSectionLeavesTheRecordingInTheRootAndCreatesNothing() {
        val dir = stopped("2026-10-23_10-15")

        assertEquals(dir, moveToSection(root, dir, "Удалённый", busy = emptySet()))
        assertEquals(dir, moveToSection(root, dir, "", busy = emptySet()))

        assertUntouched(dir)
        assertFalse(File(root, "Удалённый").exists())
        assertEquals(listOf(dir.canonicalFile), libraryFolders(root).records)
    }

    @Test fun choiceOfAVanishedOrReplacedSectionResetsLikeOnThePc() {
        section("Работа")
        stopped("Звонок")
        File(root, "Пустой").mkdirs()

        assertFalse(sectionGone(root, "Работа"))
        assertFalse(sectionGone(root, ""))
        assertTrue(sectionGone(root, "Удалённый"))
        assertTrue("became a recording", sectionGone(root, "Звонок"))
        // a section of the last sync stays a section, even empty and with recording files in it
        File(root, "Пустой/mic.wav").writeBytes(sound)
        assertFalse(sectionGone(root, "Пустой", sections = setOf("Пустой")))
        assertFalse("no root, the choice stays", sectionGone(File(tmp.root, "нет"), "Работа"))
    }

    @Test fun unreadableSectionOrRootKeepsTheChoice() {
        val base = root
        val work = section("Работа")
        val rights = Files.getPosixFilePermissions(base.toPath())
        try {
            Files.setPosixFilePermissions(work.toPath(), emptySet())
            assumeFalse("root reads a closed folder too", work.canRead())
            assertFalse("the section is unreadable", sectionGone(base, "Работа"))
            Files.setPosixFilePermissions(base.toPath(), emptySet())
            assertFalse("the root is unreadable", sectionGone(base, "Работа"))
            assertFalse("the root is unreadable", sectionGone(base, "Удалённый"))
        } finally {
            Files.setPosixFilePermissions(base.toPath(), rights)
            Files.setPosixFilePermissions(work.toPath(), rights)
        }
        assertTrue(sectionGone(base, "Удалённый"))
    }

    @Test fun failedMoveKeepsTheRecordingInTheRoot() {
        val dir = stopped("2026-10-23_10-15")
        val work = section("Работа")
        assertTrue(work.setWritable(false))
        try {
            assumeFalse("root writes into a closed folder too", work.canWrite())
            assertEquals(dir, moveToSection(root, dir, "Работа", busy = emptySet()))
        } finally { work.setWritable(true) }

        assertUntouched(dir)
        assertEquals(emptyList<String>(), work.list()!!.toList())
    }

    @Test fun recordingBeingWrittenOrProcessedIsNeverMoved() {
        section("Работа")
        val writing = stopped("2026-10-23_10-15")
        val processing = stopped("2026-10-23_11-00", id = "processing-id")
        ProcessStateFile.write(processing, ProcessState(id = "processing-id", stage = ProcessStage.ASR))
        val busy = busyRecordings(root.canonicalFile, writing.name, hasWork = { true })

        assertEquals(writing, moveToSection(root, writing, "Работа", busy))
        assertEquals(processing, moveToSection(root, processing, "Работа", busy))
        assertEquals(processing, moveToSection(root, processing, "Работа", busy = setOf("processing-id")))

        assertUntouched(writing)
        assertUntouched(processing)
        assertEquals(emptyList<String>(), File(root, "Работа").list()!!.toList())
    }

    @Test fun onlyARealSectionOfTheRootAcceptsTheRecording() {
        val dir = stopped("2026-10-23_10-15")
        stopped("Звонок")
        section(".obsidian")
        val outside = File(tmp.root, "Чужая").apply { mkdirs() }
        Files.createSymbolicLink(File(root, "Ссылка").toPath(), outside.toPath())

        for (name in listOf("Звонок", ".obsidian", "..", "Ссылка", "Работа/Дейли", "2026-10-23_10-15")) {
            assertEquals(name, dir, moveToSection(root, dir, name, busy = emptySet()))
        }

        assertUntouched(dir)
        assertEquals(emptyList<String>(), outside.list()!!.toList())
    }

    @Test fun recordingAlreadyInASectionStaysWhereItIs() {
        section("Личное")
        section("Работа")
        val dir = stopped("Личное/2026-10-23_10-15")

        assertEquals(dir, moveToSection(root, dir, "Работа", busy = emptySet()))
        assertUntouched(dir)
    }

    // "Move to section" from the card menu

    private fun store(writing: () -> String? = { null }, working: (String) -> Boolean = { false }) =
        FolderRecordingStore(root, CoroutineScope(Dispatchers.Unconfined), writing, working, Dispatchers.Unconfined)

    @Test fun recordingMovesBetweenSectionsAndBackToTheRootKeepingItsIdAndFiles() {
        section("Работа")
        section("Личное")
        val dir = stopped("Работа/2026-10-23_10-15", id = "record-id")

        val personal = moveRecording(root, dir, "Личное")
        assertEquals(File(root.canonicalFile, "Личное/2026-10-23_10-15"), personal)
        assertUntouched(personal)
        assertEquals("record-id", recordingId(personal))

        val home = moveRecording(root, personal, "")
        assertEquals(File(root.canonicalFile, "2026-10-23_10-15"), home)
        assertUntouched(home)
        assertEquals(listOf(home), libraryFolders(root).records)
        // emptied sections stay sections
        assertEquals(listOf("Личное", "Работа"), libraryFolders(root).sections)
        assertEquals("record-id", findRecording(root, "record-id")?.let(::recordingId))
    }

    @Test fun sectionTheRecordIsAlreadyInChangesNothing() {
        section("Работа")
        val inSection = stopped("Работа/2026-10-23_10-15")
        val inRoot = stopped("2026-10-23_11-00")

        assertEquals(inSection.canonicalFile, moveRecording(root, inSection, "Работа"))
        assertEquals(inRoot.canonicalFile, moveRecording(root, inRoot, ""))
        assertUntouched(inSection)
        assertUntouched(inRoot)
    }

    @Test fun takenNameGetsANumberWhenMovingOutOfASection() {
        section("Работа")
        stopped("2026-10-23_10-15", id = "root")
        val dir = stopped("Работа/2026-10-23_10-15", id = "moved")

        val moved = moveRecording(root, dir, "")

        assertEquals("2026-10-23_10-15 (2)", moved.name)
        assertEquals(root.canonicalFile, moved.parentFile)
        assertEquals("moved", recordingId(moved))
    }

    @Test fun newSectionIsCreatedWithACleanNameAndTheRecordMovesIntoIt() {
        val dir = stopped("2026-10-23_10-15", id = "record-id")

        val moved = moveRecording(root, dir, "  Клиенты: Q4 / план.  ", create = true)

        assertEquals(File(root.canonicalFile, "Клиенты Q4 план/2026-10-23_10-15"), moved)
        assertUntouched(moved)
        assertEquals(listOf("Клиенты Q4 план"), libraryFolders(root).sections)
        assertEquals("Клиенты Q4 план", sectionName("  Клиенты: Q4 / план.  "))
        assertEquals("", sectionName(" ?*. "))
    }

    @Test fun newSectionWithALongMultiByteNameIsCutToTheFolderNameLimit() {
        val dir = stopped("2026-10-23_10-15", id = "record-id")
        // 70 four-byte emoji, 280 bytes, over the file system name limit
        val typed = "😀".repeat(70)

        val moved = moveRecording(root, dir, typed, create = true)

        val section = moved.parentFile!!
        assertEquals(root.canonicalFile, section.parentFile)
        assertEquals(sectionName(typed), section.name)
        assertEquals("😀".repeat(60), section.name)
        assertUntouched(moved)
        assertEquals("record-id", recordingId(moved))
    }

    @Test fun newSectionNamedLikeAnExistingOneInAnotherCaseIsThatSection() {
        val work = section("Работа")
        val dir = stopped("2026-10-23_10-15")

        val moved = moveRecording(root, dir, "работа", create = true)

        assertEquals(File(work.canonicalFile, "2026-10-23_10-15"), moved)
        assertEquals(listOf("Работа"), root.list()!!.toList())
    }

    @Test fun newSectionCannotTakeTheNameOfARecordOrBeEmpty() {
        val other = stopped("Звонок")
        val dir = stopped("2026-10-23_10-15")

        val taken = assertThrows(IOException::class.java) { moveRecording(root, dir, "звонок", create = true) }
        assertEquals("Это название занято", taken.message)
        assertThrows(IOException::class.java) { moveRecording(root, dir, " ... ", create = true) }

        assertUntouched(dir)
        assertUntouched(other)
        assertEquals(setOf("Звонок", "2026-10-23_10-15"), root.list()!!.toSet())
    }

    @Test fun failedMoveIntoANewSectionLeavesNoEmptyFolder() {
        val dir = stopped("2026-10-23_10-15")
        val rights = Files.getPosixFilePermissions(dir.toPath())
        // a folder without write permission cannot move to another folder (its ".." changes)
        Files.setPosixFilePermissions(dir.toPath(), setOf(java.nio.file.attribute.PosixFilePermission.OWNER_READ, java.nio.file.attribute.PosixFilePermission.OWNER_EXECUTE))
        try {
            assumeFalse("root moves a closed folder too", Files.isWritable(dir.toPath()))
            val failed = try { moveRecording(root, dir, "Работа", create = true); null } catch (e: IOException) { e }
            assumeFalse("the system moves a closed folder", failed == null)
            assertEquals("Не удалось переместить запись", failed!!.message)
        } finally { Files.setPosixFilePermissions(dir.toPath(), rights) }

        assertUntouched(dir)
        assertFalse(File(root, "Работа").exists())
    }

    @Test fun moveErrorsAreEnglishInAnEnglishInterface() {
        val dir = stopped("2026-10-23_10-15")
        val before = Ui.lang
        Ui.lang = UiLang.EN
        try {
            assertEquals("The section no longer exists", assertThrows(IOException::class.java) { moveRecording(root, dir, "Удалённый") }.message)
            assertEquals("Enter a section name", assertThrows(IOException::class.java) { moveRecording(root, dir, " ??? ", create = true) }.message)
            assertEquals("The recording folder is unavailable", assertThrows(IOException::class.java) { moveRecording(root, File(root, "Пропавшая"), "") }.message)
        } finally { Ui.lang = before }
        assertUntouched(dir)
    }

    @Test fun vanishedRecordOrSectionIsAnErrorAndNothingMoves() {
        val dir = stopped("2026-10-23_10-15")

        assertEquals("Раздела больше нет", assertThrows(IOException::class.java) { moveRecording(root, dir, "Удалённый") }.message)
        assertThrows(IOException::class.java) { moveRecording(root, File(root, "Пропавшая"), "") }

        assertUntouched(dir)
        assertFalse(File(root, "Удалённый").exists())
    }

    @Test fun storeRefusesToMoveARecordingBeingWrittenOrProcessed() = runBlocking {
        section("Работа")
        val writing = stopped("2026-10-23_10-15")
        val processing = stopped("2026-10-23_11-00", id = "processing-id")
        ProcessStateFile.write(processing, ProcessState(id = "processing-id", stage = ProcessStage.ASR))
        val store = store(writing = { writing.name }, working = { true })

        assertNull(store.move(writing, "Работа"))
        assertEquals("Запись занята; дождитесь завершения", store.failure.value)
        assertNull(store.move(processing, "Работа", create = false))
        assertNull(store.move(processing, "Новый", create = true))

        assertUntouched(writing)
        assertUntouched(processing)
        assertEquals(emptyList<String>(), File(root, "Работа").list()!!.toList())
        assertFalse(File(root, "Новый").exists())
    }

    @Test fun storeMovesARecordAndTheListShowsItInTheNewSection() = runBlocking {
        section("Работа")
        val dir = stopped("2026-10-23_10-15", id = "record-id")
        val store = store()

        val moved = store.move(dir, "Работа")!!

        assertNull(store.failure.value)
        val item = store.items.value.single()
        assertEquals("Работа", item.section)
        assertEquals("record-id", item.id)
        assertEquals(moved, item.dir)
        assertNull("a missing recording", store.move(File(root, "нет"), ""))
        assertEquals("Папка записи недоступна", store.failure.value)
    }

    @Test fun pickerOffersNoSectionThenSectionsByName() {
        assertEquals(listOf("", "Личное", "Работа"), sectionChoices(listOf("Работа", "Личное")))
        assertEquals(listOf(""), sectionChoices(emptyList()))
    }
}
