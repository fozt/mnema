package dev.mnema.app.helper

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.Closeable
import java.io.IOException
import java.lang.reflect.InvocationTargetException

class FailuresTest {
    private val failures = Failures()

    @Test
    fun firstErrorKeepsItsStageAsReason() {
        failures.error("микрофон недоступен", IOException("first"))
        failures.error("файлы записи не закрылись", IOException("second"))

        assertEquals("микрофон недоступен", failures.reason())
        assertEquals(listOf("java.io.IOException: first", "java.io.IOException: second"), failures.errors())
    }

    @Test
    fun warningDoesNotSpoilRecording() {
        failures.warning(IllegalStateException("routed device"))

        assertNull(failures.reason())
        assertTrue(failures.errors().isEmpty())
        assertEquals(listOf("java.lang.IllegalStateException: routed device"), failures.warnings())
    }

    @Test
    fun fileThatDoesNotCloseIsErrorAboutFiles() {
        val broken = Closeable { throw IOException("disk full") }

        val cause = failures.close(broken)

        assertEquals("файлы записи не сохранились до конца", helperText(failures.reason()!!))
        assertEquals("disk full", cause?.message)
        assertEquals(listOf("java.io.IOException: disk full"), failures.errors())
    }

    @Test
    fun closedOrMissingFileIsNotAFailure() {
        var closed = false

        assertNull(failures.close(Closeable { closed = true }))
        assertNull(failures.close(null))

        assertTrue(closed)
        assertNull(failures.reason())
        assertTrue(failures.errors().isEmpty())
    }

    @Test
    fun reflectionWrapperIsUnwrapped() {
        val root = IllegalArgumentException("register failed")
        failures.error("нет доступа к звуку звонка", InvocationTargetException(root))
        failures.warning(InvocationTargetException(InvocationTargetException(root)))

        assertEquals(listOf("java.lang.IllegalArgumentException: register failed"), failures.errors())
        assertEquals(listOf("java.lang.IllegalArgumentException: register failed"), failures.warnings())
        assertSame(root, Failures.root(InvocationTargetException(root)))
    }
}
