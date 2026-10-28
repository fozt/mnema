package dev.mnema.app.helper

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class HelperLogTest {
    @get:Rule
    val folder = TemporaryFolder()

    @Test
    fun tailKeepsTheLastWholeLines() {
        val log = File(folder.root, "mnema-helper.log")
        log.writeText((1..500).joinToString("") { "строка $it\n" })

        assertEquals((498..500).joinToString("") { "строка $it\n" }, String(HelperLog.tail(log, 64 * 1024, 3)!!))
        assertEquals("a line cut at the front is dropped", "строка 499\nстрока 500\n", String(HelperLog.tail(log, 40, 300)!!))
        assertNull(HelperLog.tail(File(folder.root, "missing.log"), 1024, 10))
    }
}
