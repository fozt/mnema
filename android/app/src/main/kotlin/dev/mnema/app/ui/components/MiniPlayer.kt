package dev.mnema.app.ui.components

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Close
import androidx.compose.material.icons.rounded.Pause
import androidx.compose.material.icons.rounded.PlayArrow
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.mnema.app.i18n.tr

private val PLAY: String get() = tr("Play")
private val PAUSE: String get() = tr("Pause")
private val CLOSE: String get() = tr("Close")

/** Above the tabs: title, pause and close; a tap opens the recording. Without [onClose] it is the player of its own recording. */
@Composable
fun MiniPlayer(title: String, player: TrackPlayback, onOpen: () -> Unit, onClose: (() -> Unit)?, modifier: Modifier = Modifier) {
    val colors = MaterialTheme.colorScheme
    Surface(
        onClick = onOpen,
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 8.dp),
        shape = MaterialTheme.shapes.large,
        color = colors.secondaryContainer,
        contentColor = colors.onSecondaryContainer,
    ) {
        Column {
            Row(Modifier.padding(start = 16.dp, end = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(
                    title,
                    modifier = Modifier.weight(1f),
                    style = MaterialTheme.typography.titleSmall,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                IconButton(onClick = { player.toggle(!player.playing) }, enabled = !player.failed) {
                    Icon(
                        if (player.playing) Icons.Rounded.Pause else Icons.Rounded.PlayArrow,
                        contentDescription = if (player.playing) PAUSE else PLAY,
                    )
                }
                onClose?.let { IconButton(onClick = it) { Icon(Icons.Rounded.Close, contentDescription = CLOSE) } }
            }
            LinearProgressIndicator(
                progress = { listened(player.positionMs, player.durationMs) },
                modifier = Modifier.fillMaxWidth(),
                color = colors.primary,
                trackColor = colors.primary.copy(alpha = TRACK_ALPHA),
                strokeCap = StrokeCap.Butt,
                gapSize = 0.dp,
                drawStopIndicator = {},
            )
        }
    }
}

/** Played share for the mini player bar; unknown length gives 0. */
fun listened(positionMs: Long, durationMs: Long): Float =
    if (durationMs <= 0) 0f else (positionMs.toFloat() / durationMs).coerceIn(0f, 1f)

private const val TRACK_ALPHA = 0.24f
