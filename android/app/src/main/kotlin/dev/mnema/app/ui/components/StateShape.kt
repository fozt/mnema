package dev.mnema.app.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.Notes
import androidx.compose.material.icons.rounded.Check
import androidx.compose.material.icons.rounded.CloudOff
import androidx.compose.material.icons.rounded.GraphicEq
import androidx.compose.material.icons.rounded.PriorityHigh
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Icon
import androidx.compose.material3.LoadingIndicator
import androidx.compose.material3.MaterialShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.toShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.graphics.shapes.RoundedPolygon
import dev.mnema.app.recordings.ProcessingState

// Shape, color and icon of a state; shape = null means processing, shown as a LoadingIndicator
private class StateLook(val shape: RoundedPolygon?, val color: Color, val onColor: Color, val icon: ImageVector?)

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
private fun stateLook(state: ProcessingState, colors: ColorScheme): StateLook = when (state) {
    ProcessingState.Preparing, is ProcessingState.Uploading, ProcessingState.Queued, is ProcessingState.Recognizing,
    ProcessingState.Summarizing ->
        StateLook(null, colors.primary, colors.onPrimary, null)
    ProcessingState.WaitingNetwork ->
        StateLook(MaterialShapes.Clover4Leaf, colors.tertiary, colors.onTertiary, Icons.Rounded.CloudOff)
    ProcessingState.Done -> StateLook(MaterialShapes.Sunny, colors.primary, colors.onPrimary, Icons.Rounded.Check)
    is ProcessingState.NoSummary ->
        StateLook(MaterialShapes.Sunny, colors.secondary, colors.onSecondary, Icons.AutoMirrored.Rounded.Notes)
    is ProcessingState.Failed -> StateLook(MaterialShapes.Gem, colors.error, colors.onError, Icons.Rounded.PriorityHigh)
    // not processed, and states this screen does not know yet, are neutral
    else -> StateLook(MaterialShapes.Clover4Leaf, colors.outline, colors.surface, Icons.Rounded.GraphicEq)
}

// size as the second parameter, as in the contract: StateShape(state, 40.dp)
@Suppress("ModifierParameter")
@Composable
fun StateShape(state: ProcessingState, size: Dp = 32.dp, modifier: Modifier = Modifier) {
    StateMark(state, withIcon = true, modifier.size(size))
}

@Composable
fun StateBadge(state: ProcessingState, modifier: Modifier = Modifier) {
    StateMark(state, withIcon = false, modifier.size(BADGE_SIZE))
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun StateMark(state: ProcessingState, withIcon: Boolean, modifier: Modifier) {
    val look = stateLook(state, MaterialTheme.colorScheme)
    val polygon = look.shape
    if (polygon == null) {
        val progress = progressOf(state)
        if (progress == null) {
            LoadingIndicator(modifier, color = look.color)
        } else {
            LoadingIndicator(progress = { progress.toFloat() }, modifier = modifier, color = look.color)
        }
        return
    }
    // a still shape of the same size as the one inside LoadingIndicator
    Box(modifier, contentAlignment = Alignment.Center) {
        Box(
            Modifier.fillMaxSize(SHAPE_FRACTION).background(look.color, polygon.toShape()),
            contentAlignment = Alignment.Center,
        ) {
            val icon = look.icon
            if (withIcon && icon != null) {
                Icon(icon, null, Modifier.fillMaxSize(ICON_FRACTION), tint = look.onColor)
            }
        }
    }
}

/** Share of the processing done, when known: uploading audio and recognition on the server. */
fun progressOf(state: ProcessingState): Double? = when (state) {
    is ProcessingState.Uploading -> state.progress
    is ProcessingState.Recognizing -> state.progress
    else -> null
}?.takeUnless { it.isNaN() }?.coerceIn(0.0, 1.0)

private val BADGE_SIZE = 24.dp
private const val SHAPE_FRACTION = 0.8f
private const val ICON_FRACTION = 0.55f
