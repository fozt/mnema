package dev.mnema.app.ui.components

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.progressSemantics
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.withFrameMillis
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.flow.collectLatest
import kotlin.math.abs
import kotlin.math.exp

fun smoothLevel(previous: Float, target: Float, dtMs: Long, tauMs: Long = 150): Float {
    val from = unit(previous)
    val to = unit(target)
    if (dtMs <= 0) return from
    if (tauMs <= 0) return to
    val step = 1f - exp(-dtMs.toFloat() / tauMs)
    return unit(from + (to - from) * step)
}

private fun unit(value: Float): Float = if (value.isNaN()) 0f else value.coerceIn(0f, 1f)

@Composable
fun LevelBar(level: Float, color: Color, modifier: Modifier = Modifier, label: String? = null) {
    val target by rememberUpdatedState(unit(level))
    var shown by remember { mutableFloatStateOf(target) }
    LaunchedEffect(Unit) {
        snapshotFlow { target }.collectLatest { goal ->
            var last = withFrameMillis { it }
            while (abs(shown - goal) > SETTLED) {
                withFrameMillis { now ->
                    shown = smoothLevel(shown, goal, now - last)
                    last = now
                }
            }
            shown = goal
        }
    }
    val track = MaterialTheme.colorScheme.surfaceContainerHighest
    Column(modifier, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        if (label != null) {
            Text(label, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(
            Modifier
                .fillMaxWidth()
                .height(BAR_HEIGHT)
                .progressSemantics(target)
                .drawBehind {
                    val radius = CornerRadius(size.height / 2)
                    drawRoundRect(track, cornerRadius = radius)
                    val filled = size.width * shown
                    if (filled > 0f) {
                        val corner = minOf(size.height, filled) / 2
                        drawRoundRect(color, size = Size(filled, size.height), cornerRadius = CornerRadius(corner))
                    }
                },
        )
    }
}

private const val SETTLED = 0.002f
private val BAR_HEIGHT = 12.dp
