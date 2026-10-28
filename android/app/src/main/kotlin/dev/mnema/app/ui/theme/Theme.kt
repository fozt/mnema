package dev.mnema.app.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialExpressiveTheme
import androidx.compose.material3.MotionScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.platform.LocalContext
import dev.mnema.app.settings.ThemeMode

fun isDark(mode: ThemeMode, systemDark: Boolean): Boolean = when (mode) {
    ThemeMode.SYSTEM -> systemDark
    ThemeMode.LIGHT -> false
    ThemeMode.DARK -> true
}

@Composable
fun MnemaTheme(mode: ThemeMode, dynamicColor: Boolean = false, content: @Composable () -> Unit) {
    val context = LocalContext.current
    val dark = isDark(mode, isSystemInDarkTheme())
    val colors = when {
        dynamicColor && dark -> dynamicDarkColorScheme(context)
        dynamicColor -> dynamicLightColorScheme(context)
        dark -> MnemaColors.dark
        else -> MnemaColors.light
    }
    MaterialExpressiveTheme(
        colorScheme = colors,
        motionScheme = MotionScheme.expressive(),
        shapes = MnemaShapes,
        typography = MnemaTypography,
        content = content,
    )
}
