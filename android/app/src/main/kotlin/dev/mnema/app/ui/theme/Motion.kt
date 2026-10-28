package dev.mnema.app.ui.theme

import androidx.compose.animation.EnterTransition
import androidx.compose.animation.ExitTransition
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.material3.MotionScheme
import androidx.compose.ui.unit.dp

object MnemaMotion {
    val SharedAxisOffset = 30.dp
    const val RECORD_TURN_MS = 12_000
    const val PULSE_MS = 1_600

    // Shared axis X: forward, the new screen comes from the right; back, from the left
    fun sharedAxisEnter(motion: MotionScheme, offsetPx: Int, forward: Boolean): EnterTransition =
        slideInHorizontally(motion.fastSpatialSpec()) { if (forward) offsetPx else -offsetPx } +
            fadeIn(motion.slowEffectsSpec())

    fun sharedAxisExit(motion: MotionScheme, offsetPx: Int, forward: Boolean): ExitTransition =
        slideOutHorizontally(motion.fastSpatialSpec()) { if (forward) -offsetPx else offsetPx } +
            fadeOut(motion.slowEffectsSpec())
}
