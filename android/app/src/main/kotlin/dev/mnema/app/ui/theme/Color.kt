package dev.mnema.app.ui.theme

import androidx.compose.material3.ColorScheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.compositeOver
import androidx.compose.ui.graphics.luminance

// Brand schemes from seed #6750A4: violet, indigo and coral pink
object MnemaColors {
    val light: ColorScheme = lightColorScheme(
        primary = Color(0xFF6750A4),
        onPrimary = Color(0xFFFFFFFF),
        primaryContainer = Color(0xFFEADDFF),
        onPrimaryContainer = Color(0xFF4F378B),
        inversePrimary = Color(0xFFD0BCFF),
        secondary = Color(0xFF3F5E95),
        onSecondary = Color(0xFFFFFFFF),
        secondaryContainer = Color(0xFFD9E2FF),
        onSecondaryContainer = Color(0xFF23477B),
        tertiary = Color(0xFF9F3C54),
        onTertiary = Color(0xFFFFFFFF),
        tertiaryContainer = Color(0xFFFFD9DE),
        onTertiaryContainer = Color(0xFF83213D),
        background = Color(0xFFFBF8FF),
        onBackground = Color(0xFF1D1A22),
        surface = Color(0xFFFBF8FF),
        onSurface = Color(0xFF1D1A22),
        surfaceVariant = Color(0xFFE7DFF4),
        onSurfaceVariant = Color(0xFF4A4455),
        surfaceTint = Color(0xFF6750A4),
        inverseSurface = Color(0xFF322F37),
        inverseOnSurface = Color(0xFFF3EFFA),
        error = Color(0xFFB3261E),
        onError = Color(0xFFFFFFFF),
        errorContainer = Color(0xFFF9DEDC),
        onErrorContainer = Color(0xFF8C1D18),
        outline = Color(0xFF7B7486),
        outlineVariant = Color(0xFFCBC3D7),
        scrim = Color(0xFF000000),
        surfaceBright = Color(0xFFFBF8FF),
        surfaceContainer = Color(0xFFF0ECF7),
        surfaceContainerHigh = Color(0xFFEBE6F1),
        surfaceContainerHighest = Color(0xFFE5E1EC),
        surfaceContainerLow = Color(0xFFF6F2FD),
        surfaceContainerLowest = Color(0xFFFFFFFF),
        surfaceDim = Color(0xFFDCD8E3),
        primaryFixed = Color(0xFFEADDFF),
        primaryFixedDim = Color(0xFFD0BCFF),
        onPrimaryFixed = Color(0xFF21005D),
        onPrimaryFixedVariant = Color(0xFF4F378B),
        secondaryFixed = Color(0xFFD9E2FF),
        secondaryFixedDim = Color(0xFFB0C6FF),
        onSecondaryFixed = Color(0xFF001C3B),
        onSecondaryFixedVariant = Color(0xFF23477B),
        tertiaryFixed = Color(0xFFFFD9DE),
        tertiaryFixedDim = Color(0xFFFFB2BE),
        onTertiaryFixed = Color(0xFF3F0015),
        onTertiaryFixedVariant = Color(0xFF83213D),
    )
    val dark: ColorScheme = darkColorScheme(
        primary = Color(0xFFD0BCFF),
        onPrimary = Color(0xFF381E72),
        primaryContainer = Color(0xFF4F378B),
        onPrimaryContainer = Color(0xFFEADDFF),
        inversePrimary = Color(0xFF6750A4),
        secondary = Color(0xFFB0C6FF),
        onSecondary = Color(0xFF00315F),
        secondaryContainer = Color(0xFF23477B),
        onSecondaryContainer = Color(0xFFD9E2FF),
        tertiary = Color(0xFFFFB2BE),
        onTertiary = Color(0xFF660027),
        tertiaryContainer = Color(0xFF762F3F),
        onTertiaryContainer = Color(0xFFFFD9DE),
        background = Color(0xFF15121A),
        onBackground = Color(0xFFE5E1EC),
        surface = Color(0xFF15121A),
        onSurface = Color(0xFFE5E1EC),
        surfaceVariant = Color(0xFF4A4455),
        onSurfaceVariant = Color(0xFFCBC3D7),
        surfaceTint = Color(0xFFD0BCFF),
        inverseSurface = Color(0xFFE5E1EC),
        inverseOnSurface = Color(0xFF322F37),
        error = Color(0xFFF2B8B5),
        onError = Color(0xFF601410),
        errorContainer = Color(0xFF8C1D18),
        onErrorContainer = Color(0xFFF9DEDC),
        outline = Color(0xFF958EA0),
        outlineVariant = Color(0xFF4A4455),
        scrim = Color(0xFF000000),
        surfaceBright = Color(0xFF3B3840),
        surfaceContainer = Color(0xFF211E26),
        surfaceContainerHigh = Color(0xFF2C2931),
        surfaceContainerHighest = Color(0xFF36333C),
        surfaceContainerLow = Color(0xFF1D1A22),
        surfaceContainerLowest = Color(0xFF110C16),
        surfaceDim = Color(0xFF15121A),
        primaryFixed = Color(0xFFEADDFF),
        primaryFixedDim = Color(0xFFD0BCFF),
        onPrimaryFixed = Color(0xFF21005D),
        onPrimaryFixedVariant = Color(0xFF4F378B),
        secondaryFixed = Color(0xFFD9E2FF),
        secondaryFixedDim = Color(0xFFB0C6FF),
        onSecondaryFixed = Color(0xFF001C3B),
        onSecondaryFixedVariant = Color(0xFF23477B),
        tertiaryFixed = Color(0xFFFFD9DE),
        tertiaryFixedDim = Color(0xFFFFB2BE),
        onTertiaryFixed = Color(0xFF3F0015),
        onTertiaryFixedVariant = Color(0xFF83213D),
    )
}

// WCAG contrast; a translucent color is first laid over the background
fun contrastRatio(foreground: Color, background: Color): Double {
    val front = foreground.compositeOver(background).luminance().toDouble()
    val back = background.luminance().toDouble()
    return (maxOf(front, back) + 0.05) / (minOf(front, back) + 0.05)
}
