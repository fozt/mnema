package dev.mnema.app.ui

import androidx.compose.material3.ColorScheme
import androidx.compose.ui.graphics.Color
import dev.mnema.app.ui.theme.MnemaColors
import dev.mnema.app.ui.theme.contrastRatio
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ColorsTest {
    private val schemes = mapOf("светлая" to MnemaColors.light, "тёмная" to MnemaColors.dark)

    @Test
    fun textOnBrandColorsIsReadable() {
        val pairs: List<Pair<String, (ColorScheme) -> Pair<Color, Color>>> = listOf(
            "onPrimary/primary" to { it.onPrimary to it.primary },
            "onSurface/surface" to { it.onSurface to it.surface },
            "onPrimaryContainer/primaryContainer" to { it.onPrimaryContainer to it.primaryContainer },
            "onSecondaryContainer/secondaryContainer" to { it.onSecondaryContainer to it.secondaryContainer },
            "onTertiaryContainer/tertiaryContainer" to { it.onTertiaryContainer to it.tertiaryContainer },
            "onSurfaceVariant/surfaceContainerHighest" to { it.onSurfaceVariant to it.surfaceContainerHighest },
        )
        for ((schemeName, scheme) in schemes) {
            for ((pairName, pick) in pairs) {
                val (foreground, background) = pick(scheme)
                val ratio = contrastRatio(foreground, background)
                assertTrue("$schemeName $pairName: $ratio", ratio >= 4.5)
            }
        }
    }

    @Test
    fun blackOnWhiteIsTheMaximum() {
        assertEquals(21.0, contrastRatio(Color.Black, Color.White), 0.01)
        assertEquals(21.0, contrastRatio(Color.White, Color.Black), 0.01)
        assertEquals("цвет на самом себе", 1.0, contrastRatio(Color(0xFF6750A4), Color(0xFF6750A4)), 0.001)
    }

    @Test
    fun translucentForegroundIsBlendedIntoBackground() {
        assertEquals(1.0, contrastRatio(Color.White.copy(alpha = 0f), Color.Black), 0.001)
        val half = contrastRatio(Color.White.copy(alpha = 0.5f), Color.Black)
        assertTrue("$half", half > 1.0 && half < 21.0)
    }
}
