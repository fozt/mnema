package dev.mnema.app.helper

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SourcesTest {
    @Test
    fun bothTracksAreRecordedUntilSwitchedOff() {
        val sources = Sources()
        assertTrue(sources.mic())
        assertTrue(sources.call())

        assertTrue(sources.apply("mic-off"))
        assertFalse(sources.mic())
        assertTrue(sources.call())

        assertTrue(sources.apply("call-off"))
        assertFalse(sources.call())

        assertTrue(sources.apply("mic-on"))
        assertTrue(sources.apply("call-on"))
        assertTrue(sources.mic())
        assertTrue(sources.call())
    }
}
