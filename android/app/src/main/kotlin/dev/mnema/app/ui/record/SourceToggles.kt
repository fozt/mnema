package dev.mnema.app.ui.record

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Headphones
import androidx.compose.material.icons.rounded.HeadsetOff
import androidx.compose.material.icons.rounded.Mic
import androidx.compose.material.icons.rounded.MicOff
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.mnema.app.ui.components.LevelBar
import dev.mnema.app.transcript.RecordingFolder

// caption of a source track; conversation bubbles stay "Me" and "Others"
internal fun sourceName(source: Source): String = RecordingFolder.TRACK_NAMES.getValue(
    when (source) {
        Source.CALL -> "system"
        Source.MIC -> "mic"
    },
)

/** Source chips under the record button; while recording each one shows its level. */
@Composable
internal fun SourceToggles(ui: RecordUi, showLevels: Boolean, onToggle: (Source) -> Unit, modifier: Modifier = Modifier) {
    val colors = MaterialTheme.colorScheme
    Row(modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        SourceColumn(
            source = SourceLook(sourceName(Source.CALL), Icons.Rounded.Headphones, Icons.Rounded.HeadsetOff, colors.tertiaryContainer, colors.onTertiaryContainer, colors.tertiary),
            on = ui.callOn,
            level = ui.callLevel,
            sound = ui.callSound,
            showLevel = showLevels,
            onToggle = { onToggle(Source.CALL) },
            modifier = Modifier.weight(1f),
        )
        SourceColumn(
            source = SourceLook(sourceName(Source.MIC), Icons.Rounded.Mic, Icons.Rounded.MicOff, colors.primaryContainer, colors.onPrimaryContainer, colors.primary),
            on = ui.micOn,
            level = ui.micLevel,
            sound = ui.micSound,
            showLevel = showLevels,
            onToggle = { onToggle(Source.MIC) },
            modifier = Modifier.weight(1f),
        )
    }
}

private class SourceLook(
    val label: String,
    val icon: ImageVector,
    val offIcon: ImageVector,
    val container: Color,
    val content: Color,
    val bar: Color,
)

@Composable
private fun SourceColumn(
    source: SourceLook,
    on: Boolean,
    level: Float,
    sound: TrackSound?,
    showLevel: Boolean,
    onToggle: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = MaterialTheme.colorScheme
    Column(modifier, verticalArrangement = Arrangement.spacedBy(12.dp)) {
        SourceChip(source, on, onToggle)
        AnimatedVisibility(showLevel) {
            Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(6.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                LevelBar(level, source.bar)
                sound?.let {
                    Text(
                        it.label,
                        style = MaterialTheme.typography.labelLarge,
                        color = when (it) {
                            TrackSound.BLOCKED -> colors.error
                            TrackSound.SILENCE -> colors.onSurfaceVariant
                            TrackSound.WRITING -> colors.onSurface
                        },
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun SourceChip(source: SourceLook, on: Boolean, onToggle: () -> Unit) {
    val colors = MaterialTheme.colorScheme
    val effects = MaterialTheme.motionScheme.defaultEffectsSpec<Color>()
    val fill by animateColorAsState(if (on) source.container else Color.Transparent, effects, label = "fill")
    val tint by animateColorAsState(if (on) source.content else colors.onSurfaceVariant, effects, label = "tint")
    val haptics = LocalHapticFeedback.current
    val outline = colors.outline
    Surface(
        checked = on,
        onCheckedChange = { checked ->
            haptics.performHapticFeedback(if (checked) HapticFeedbackType.ToggleOn else HapticFeedbackType.ToggleOff)
            onToggle()
        },
        modifier = Modifier
            .fillMaxWidth()
            .then(if (on) Modifier else Modifier.dashedOutline(outline)),
        shape = CircleShape,
        color = fill,
        contentColor = tint,
    ) {
        Row(
            Modifier
                .heightIn(min = CHIP_HEIGHT)
                .padding(horizontal = 12.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.CenterHorizontally),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(if (on) source.icon else source.offIcon, contentDescription = null)
            Text(source.label, style = MaterialTheme.typography.labelLarge, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
    }
}

// an off source is a dashed outline over a transparent chip
private fun Modifier.dashedOutline(color: Color): Modifier = drawWithContent {
    drawContent()
    val stroke = OUTLINE_WIDTH.toPx()
    val dash = DASH.toPx()
    drawRoundRect(
        color = color,
        topLeft = Offset(stroke / 2, stroke / 2),
        size = Size(size.width - stroke, size.height - stroke),
        cornerRadius = CornerRadius((size.height - stroke) / 2),
        style = Stroke(width = stroke, pathEffect = PathEffect.dashPathEffect(floatArrayOf(dash, dash * GAP_RATIO))),
    )
}

private const val GAP_RATIO = 0.75f
private val CHIP_HEIGHT = 56.dp
private val OUTLINE_WIDTH = 2.dp
private val DASH = 8.dp
