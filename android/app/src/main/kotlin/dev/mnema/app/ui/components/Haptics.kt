package dev.mnema.app.ui.components

import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalHapticFeedback

object MnemaHaptics {
    @Composable
    fun rememberRecordHaptics(): (start: Boolean) -> Unit {
        val haptics = LocalHapticFeedback.current
        return remember(haptics) {
            { start -> haptics.performHapticFeedback(if (start) HapticFeedbackType.Confirm else HapticFeedbackType.Reject) }
        }
    }
}
