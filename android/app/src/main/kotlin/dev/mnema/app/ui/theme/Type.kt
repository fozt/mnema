package dev.mnema.app.ui.theme

import androidx.compose.material3.Typography
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.DeviceFontFamilyName
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight

// System font: Google Sans on Pixel, the default font on other phones
private fun deviceFamily(name: String) = FontFamily(
    listOf(FontWeight.Normal, FontWeight.Medium, FontWeight.SemiBold, FontWeight.Bold).map {
        Font(DeviceFontFamilyName(name), it)
    },
)

private val brand = Typography(fontFamily = deviceFamily("google-sans"))
private val plain = Typography(fontFamily = deviceFamily("google-sans-text"))

val MnemaTypography = Typography(
    displayLarge = brand.displayLarge,
    displayMedium = brand.displayMedium,
    displaySmall = brand.displaySmall,
    headlineLarge = brand.headlineLarge,
    headlineMedium = brand.headlineMedium,
    headlineSmall = brand.headlineSmall,
    titleLarge = brand.titleLarge,
    titleMedium = brand.titleMedium,
    titleSmall = brand.titleSmall,
    bodyLarge = plain.bodyLarge,
    bodyMedium = plain.bodyMedium,
    bodySmall = plain.bodySmall,
    labelLarge = plain.labelLarge,
    labelMedium = plain.labelMedium,
    labelSmall = plain.labelSmall,
    displayLargeEmphasized = brand.displayLargeEmphasized,
    displayMediumEmphasized = brand.displayMediumEmphasized,
    displaySmallEmphasized = brand.displaySmallEmphasized,
    headlineLargeEmphasized = brand.headlineLargeEmphasized,
    headlineMediumEmphasized = brand.headlineMediumEmphasized,
    headlineSmallEmphasized = brand.headlineSmallEmphasized,
    titleLargeEmphasized = brand.titleLargeEmphasized,
    titleMediumEmphasized = brand.titleMediumEmphasized,
    titleSmallEmphasized = brand.titleSmallEmphasized,
    bodyLargeEmphasized = plain.bodyLargeEmphasized,
    bodyMediumEmphasized = plain.bodyMediumEmphasized,
    bodySmallEmphasized = plain.bodySmallEmphasized,
    labelLargeEmphasized = plain.labelLargeEmphasized,
    labelMediumEmphasized = plain.labelMediumEmphasized,
    labelSmallEmphasized = plain.labelSmallEmphasized,
)

val Typography.timer: TextStyle
    get() = displayLarge.copy(fontFeatureSettings = "tnum")
