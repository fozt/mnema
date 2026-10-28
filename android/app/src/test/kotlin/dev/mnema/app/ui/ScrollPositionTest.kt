package dev.mnema.app.ui

import dev.mnema.app.ui.components.scrollFraction
import dev.mnema.app.ui.components.scrollPosition
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ScrollPositionTest {
    @Test
    fun scrollbarMapsTheConversationAndBothEnds() {
        assertEquals(0f, scrollFraction(0, 0, 100, 100, 10, true))
        assertEquals(0.5f, scrollFraction(45, 0, 100, 100, 10, true))
        assertTrue(scrollFraction(45, 50, 100, 100, 10, true) > 0.5f)
        assertEquals(1f, scrollFraction(89, 0, 400, 100, 11, false))
        assertEquals(0, scrollPosition(0f, 100, 10))
        assertEquals(45, scrollPosition(0.5f, 100, 10))
        assertEquals(99, scrollPosition(1f, 100, 10))
    }

    @Test
    fun emptyAndShortListsNeverProduceInvalidPositions() {
        assertEquals(0f, scrollFraction(0, 0, 0, 0, 0, false))
        assertEquals(0f, scrollFraction(0, 0, 100, 5, 5, false))
        assertEquals(0, scrollPosition(Float.NaN, 100, 10))
        assertEquals(0, scrollPosition(-2f, 100, 10))
        assertEquals(99, scrollPosition(2f, 100, 10))
        assertEquals(0, scrollPosition(1f, 0, 0))
    }
}
