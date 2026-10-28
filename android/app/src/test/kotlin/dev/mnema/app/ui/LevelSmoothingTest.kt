package dev.mnema.app.ui

import dev.mnema.app.ui.components.smoothLevel
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class LevelSmoothingTest {
    @Test
    fun resultIsAlwaysWithinUnitRange() {
        val levels = listOf(-1f, 0f, 0.5f, 1f, 2f, Float.NaN)
        val elapsed = listOf(-50L, 0L, 16L, 150L, 60_000L)
        for (previous in levels) for (target in levels) for (dt in elapsed) {
            val result = smoothLevel(previous, target, dt)
            assertTrue("$previous → $target за $dt мс = $result", result in 0f..1f)
        }
        assertEquals("время не прошло — прежний уровень", 0.4f, smoothLevel(0.4f, 1f, 0), 0f)
        assertEquals("долгая пауза — цель", 0.7f, smoothLevel(0.1f, 0.7f, 10_000), 0.001f)
    }
}
