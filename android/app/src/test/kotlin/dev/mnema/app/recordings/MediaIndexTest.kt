package dev.mnema.app.recordings

import java.io.File
import java.io.IOException
import java.nio.file.FileSystemException
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class MediaIndexTest {
    @get:Rule val tmp = TemporaryFolder()

    @After fun noScanner() { MediaIndex.scanner = null }

    private fun section(): File = tmp.newFolder("projects").also { section ->
        File(section, "2026-10-27_Созвон").apply { mkdirs(); File(this, "summary.md").writeText("суть") }
        File(section, "2026-10-26_Дейли").apply { mkdirs(); File(this, "transcript.txt").writeText("текст") }
    }

    @Test
    fun refusedMoveIsRetriedOnceAfterEveryFileInsideIsIndexed() {
        val dir = section()
        val scanned = mutableListOf<Pair<Set<String>, Boolean>>()
        MediaIndex.scanner = { files, wait -> scanned += files.map { it.name }.toSet() to wait }
        var tries = 0
        val result = MediaIndex.retrying(dir) {
            if (++tries == 1) throw FileSystemException(dir.path, null, "Operation not permitted")
            "перенесено"
        }
        assertEquals("перенесено", result)
        assertEquals(2, tries)
        assertEquals(listOf(setOf("summary.md", "transcript.txt") to true), scanned)
    }

    @Test
    fun stillRefusedAfterIndexingFailsWithTheSecondRefusal() {
        val dir = section()
        MediaIndex.scanner = { _, _ -> }
        var tries = 0
        val error = assertThrows(FileSystemException::class.java) {
            MediaIndex.retrying(dir) { throw FileSystemException(dir.path, null, "отказ ${++tries}") }
        }
        assertTrue(error.message!!.contains("отказ 2"))
    }

    @Test
    fun vanishedFolderOrNoScannerIsNotRetried() {
        val dir = section()
        var tries = 0
        assertThrows(IOException::class.java) { MediaIndex.retrying(dir) { tries++; throw IOException("нет сканера") } }
        MediaIndex.scanner = { _, _ -> error("a vanished folder is not indexed") }
        dir.deleteRecursively()
        assertThrows(IOException::class.java) { MediaIndex.retrying(dir) { tries++; throw IOException("нет папки") } }
        assertEquals(2, tries)
    }

    @Test
    fun replacedFileIsIndexedWithoutWaiting() {
        val calls = mutableListOf<Pair<List<String>, Boolean>>()
        MediaIndex.scanner = { files, wait -> calls += files.map { it.name } to wait }
        val file = tmp.newFile("summary.md")
        dev.mnema.app.transcript.atomicWrite(file, "новая суть".toByteArray())
        assertEquals(listOf(listOf("summary.md") to false), calls)
    }
}
