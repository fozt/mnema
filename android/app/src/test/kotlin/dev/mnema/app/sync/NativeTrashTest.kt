package dev.mnema.app.sync

import java.io.File
import java.nio.file.Files
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class NativeTrashTest {
    @get:Rule val temp = TemporaryFolder()

    private fun trash(api: Int = 37, allFiles: () -> Boolean = { true }, system: (File) -> Unit) =
        NativeTrash(api, allFiles, { File(temp.root, "probe") }, system)

    @Test fun systemRefusalsBecomeUserReasonsAndKeepTheFolder() {
        val folder = temp.newFolder("record").also { File(it, "mic.wav").writeText("audio") }
        val refusals = listOf(
            IllegalStateException("trash directory cannot be created") to "Система не переместила запись в корзину",
            IllegalArgumentException("path is empty") to "Система не переместила запись в корзину",
            SecurityException("denied") to "Нет доступа к корзине. Разрешите доступ ко всем файлам",
            UnsupportedOperationException() to "Корзина недоступна",
        )
        for ((error, reason) in refusals) {
            val thrown = assertThrows(SyncException::class.java) { trash { throw error }.move(folder) }
            assertEquals(reason, thrown.message)
            assertTrue(File(folder, "mic.wav").isFile)
        }
        val ignored = assertThrows(SyncException::class.java) { trash {}.move(folder) }
        assertEquals("Система не переместила запись в корзину", ignored.message)
        assertTrue(folder.isDirectory)
    }

    @Test fun trashIsMissingBeforeAndroid17AndWithoutAccessDeletionAsksForItInsteadOfDeletingForever() {
        var asked = 0
        assertFalse(trash(api = 36) { asked++ }.available())
        var allFiles = false
        val withoutAccess = trash(allFiles = { allFiles }) { asked++; throw IllegalStateException("no such file") }
        assertTrue("without access nothing can be deleted forever, so forever is not offered", withoutAccess.available())
        assertEquals(0, asked)
        val folder = temp.newFolder("record").also { File(it, "mic.wav").writeText("audio") }
        val refused = assertThrows(SyncException::class.java) { discard(folder, forever = false, withoutAccess.available(), withoutAccess::move) }
        assertEquals("Разрешите доступ ко всем файлам для перемещения в корзину", refused.message)
        assertTrue(File(folder, "mic.wav").isFile)
        assertEquals(0, asked)
        allFiles = true
        assertTrue(withoutAccess.available())
        assertEquals(1, asked)
    }

    @Test fun unsupportedSystemTrashIsRememberedUntilRestart() {
        var asked = 0
        val missing = trash { asked++; throw UnsupportedOperationException() }
        assertFalse(missing.available())
        assertFalse(missing.available())
        assertEquals(1, asked)
        val folder = temp.newFolder("record")
        assertEquals("Корзина недоступна", assertThrows(SyncException::class.java) { missing.move(folder) }.message)
        assertEquals(1, asked)
        assertTrue(folder.isDirectory)

        // a system refusal on the probe path means the trash exists
        var checked = 0
        val present = trash { checked++; throw IllegalStateException("cannot be trashed") }
        assertTrue(present.available())
        assertTrue(present.available())
        assertEquals(1, checked)
        assertFalse(File(temp.root, "probe").exists())
    }

    @Test fun trashFoundMissingOnARealMoveIsNotOfferedAgain() {
        var supported = true
        val trash = trash { file -> if (!supported) throw UnsupportedOperationException() else if (file.name != "probe") file.deleteRecursively() }
        assertTrue(trash.available())
        supported = false
        assertThrows(SyncException::class.java) { trash.move(temp.newFolder("record")) }
        assertFalse(trash.available())
    }

    @Test fun permanentDeletionNeedsConsentAndRemovesTheFolderButNotLinkTargets() {
        val outside = temp.newFolder("outside").also { File(it, "keep.txt").writeText("keep") }
        val folder = temp.newFolder("record")
        File(folder, "nested").mkdirs()
        File(folder, "nested/mic.wav").writeText("audio")
        Files.createSymbolicLink(File(folder, "link").toPath(), outside.toPath())
        assertEquals("Корзина недоступна", assertThrows(SyncException::class.java) { discard(folder, forever = false, hasTrash = false) { error("no trash") } }.message)
        assertTrue(File(folder, "nested/mic.wav").isFile)
        discard(folder, forever = true, hasTrash = false) { error("no trash") }
        assertFalse(folder.exists())
        assertEquals("keep", File(outside, "keep.txt").readText())
        val moved = mutableListOf<File>()
        val kept = temp.newFolder("kept")
        discard(kept, forever = true, hasTrash = true) { moved += it }
        assertEquals(listOf(kept), moved)
        assertTrue(kept.isDirectory)
    }
}
