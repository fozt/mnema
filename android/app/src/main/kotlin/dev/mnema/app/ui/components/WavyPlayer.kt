package dev.mnema.app.ui.components

import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.VolumeOff
import androidx.compose.material.icons.automirrored.rounded.VolumeUp
import androidx.compose.material.icons.rounded.Check
import androidx.compose.material.icons.rounded.Pause
import androidx.compose.material.icons.rounded.PlayArrow
import androidx.compose.material.icons.rounded.Replay
import androidx.compose.material3.DropdownMenuGroup
import androidx.compose.material3.DropdownMenuPopup
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.FilledIconToggleButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.LinearWavyProgressIndicator
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MenuDefaults
import androidx.compose.material3.SelectableDropdownMenuItem
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.setProgress
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import dev.mnema.app.i18n.tr
import dev.mnema.app.transcript.RecordingFolder

private val PLAY: String get() = tr("Play")
private val PAUSE: String get() = tr("Pause")
private val SEEK: String get() = tr("Seek")
private val BACK: String get() = tr("Back 15 seconds")
private val FORWARD: String get() = tr("Forward 15 seconds")
private const val SKIP_DIGITS = "15"
private val SPEED: String get() = tr("Speed")
private val UNREADABLE: String get() = tr("The recording cannot be opened")

// Wavy seek bar, below it ±15 s and pause, then tracks and speed; [onSpeed] gets the speed picked in the menu
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun WavyPlayer(player: TrackPlayback, onSpeed: (Float) -> Unit, modifier: Modifier = Modifier, lengthMs: Long = 0) {
    var dragFraction by remember(player) { mutableStateOf<Float?>(null) }
    val playing = player.playing
    val durationMs = player.durationMs
    val shownLength = playbackLength(durationMs, lengthMs)
    val usable = !player.failed
    val seekable = usable && durationMs > 0
    val seekTo = { fraction: Float -> player.seekTo((fraction.coerceIn(0f, 1f) * durationMs).toLong()) }
    val shown = dragFraction ?: fraction(player.positionMs, durationMs)

    // on a primary-colored header the button and the bar take the inverse colors
    val colors = MaterialTheme.colorScheme
    val onPrimary = LocalContentColor.current == colors.onPrimary
    val accent = if (onPrimary) colors.onPrimary else colors.primary
    val onAccent = if (onPrimary) colors.primary else colors.onPrimary
    val times = MaterialTheme.typography.labelMedium.copy(fontFeatureSettings = "tnum")

    Column(modifier.fillMaxWidth()) {
        Box(
            Modifier
                .fillMaxWidth()
                .height(SEEK_HEIGHT)
                .semantics(mergeDescendants = true) {
                    if (seekable) {
                        setProgress(SEEK) { target ->
                            seekTo(target)
                            true
                        }
                    }
                }
                .pointerInput(seekable, player, durationMs) {
                    if (!seekable) return@pointerInput
                    detectTapGestures { seekTo(it.x / size.width) }
                }
                .pointerInput(seekable, player, durationMs) {
                    if (!seekable) return@pointerInput
                    detectHorizontalDragGestures(
                        onDragStart = { dragFraction = it.x / size.width },
                        onDragEnd = {
                            dragFraction?.let(seekTo)
                            dragFraction = null
                        },
                        onDragCancel = { dragFraction = null },
                    ) { change, _ ->
                        dragFraction = (change.position.x / size.width).coerceIn(0f, 1f)
                    }
                },
            contentAlignment = Alignment.Center,
        ) {
            LinearWavyProgressIndicator(
                progress = { shown },
                modifier = Modifier.fillMaxWidth(),
                color = accent,
                trackColor = accent.copy(alpha = TRACK_ALPHA),
                amplitude = { if (playing) 1f else 0f },
            )
        }
        Row(Modifier.fillMaxWidth()) {
            val shownMs = dragFraction?.let { (it * durationMs).toLong() } ?: player.positionMs
            Text(durationLabel(shownMs / 1000.0), style = times)
            Spacer(Modifier.weight(1f))
            Text(
                when {
                    !usable -> UNREADABLE
                    shownLength > 0 -> durationLabel(shownLength / 1000.0)
                    else -> ""
                },
                style = times,
            )
        }
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterHorizontally),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            SkipButton(forward = false, enabled = seekable, color = accent) { player.skip(-SKIP_MS) }
            FilledIconToggleButton(
                checked = playing,
                onCheckedChange = player::toggle,
                enabled = usable,
                shapes = IconButtonDefaults.toggleableShapes(),
                colors = IconButtonDefaults.filledIconToggleButtonColors(
                    containerColor = accent,
                    contentColor = onAccent,
                    checkedContainerColor = accent,
                    checkedContentColor = onAccent,
                ),
                modifier = Modifier.size(IconButtonDefaults.mediumContainerSize()),
            ) {
                Icon(
                    if (playing) Icons.Rounded.Pause else Icons.Rounded.PlayArrow,
                    contentDescription = if (playing) PAUSE else PLAY,
                    modifier = Modifier.size(IconButtonDefaults.mediumIconSize),
                )
            }
            SkipButton(forward = true, enabled = seekable, color = accent) { player.skip(SKIP_MS) }
        }
        // on a narrow screen speed wraps to the next line instead of shrinking
        FlowRow(
            Modifier
                .fillMaxWidth()
                .padding(top = 4.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
            itemVerticalAlignment = Alignment.CenterVertically,
        ) {
            // muting the only track makes no sense: that is just a pause
            if (player.labels.size > 1) {
                RecordingFolder.TRACK_NAMES.filter { it.key in player.labels }.forEach { (label, name) ->
                    TrackChip(name, on = label !in player.muted, enabled = usable) { player.toggleTrack(label) }
                }
            }
            Spacer(Modifier.weight(1f))
            SpeedChip(player.speed, enabled = usable, onSelect = onSpeed)
        }
    }
}

// a ring with an arrow and a number, like Replay10; forward is mirrored
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun SkipButton(forward: Boolean, enabled: Boolean, color: Color, onClick: () -> Unit) {
    val digits = with(LocalDensity.current) { SKIP_DIGITS_SIZE.toSp() }
    IconButton(
        onClick = onClick,
        shapes = IconButtonDefaults.shapes(),
        enabled = enabled,
        colors = IconButtonDefaults.iconButtonColors(contentColor = color, disabledContentColor = color.copy(alpha = DISABLED_ALPHA)),
        modifier = Modifier
            .size(IconButtonDefaults.mediumContainerSize())
            .semantics { contentDescription = if (forward) FORWARD else BACK },
    ) {
        Box(Modifier.size(SKIP_ICON_SIZE), contentAlignment = Alignment.Center) {
            Icon(
                Icons.Rounded.Replay,
                contentDescription = null,
                modifier = Modifier
                    .fillMaxSize()
                    .graphicsLayer { if (forward) scaleX = -1f },
            )
            Text(
                SKIP_DIGITS,
                modifier = Modifier
                    .offset(y = SKIP_ICON_SIZE / RING_CENTER_SHIFT)
                    .clearAndSetSemantics {},
                style = TextStyle(fontSize = digits, lineHeight = digits, fontWeight = FontWeight.Bold),
            )
        }
    }
}

// an enabled track is a filled chip, like the state chip in the header; a muted one is outlined
@Composable
private fun TrackChip(name: String, on: Boolean, enabled: Boolean, onClick: () -> Unit) {
    val colors = MaterialTheme.colorScheme
    val content = LocalContentColor.current
    FilterChip(
        selected = on,
        onClick = onClick,
        label = { Text(name) },
        enabled = enabled,
        leadingIcon = {
            Icon(
                if (on) Icons.AutoMirrored.Rounded.VolumeUp else Icons.AutoMirrored.Rounded.VolumeOff,
                contentDescription = null,
                modifier = Modifier.size(FilterChipDefaults.IconSize),
            )
        },
        shape = CircleShape,
        colors = FilterChipDefaults.filterChipColors(
            containerColor = Color.Transparent,
            labelColor = content,
            iconColor = content,
            selectedContainerColor = colors.surface,
            selectedLabelColor = colors.onSurface,
            selectedLeadingIconColor = colors.onSurface,
        ),
        border = FilterChipDefaults.filterChipBorder(
            enabled = enabled,
            selected = on,
            borderColor = content.copy(alpha = OUTLINE_ALPHA),
            selectedBorderColor = Color.Transparent,
        ),
    )
}

// a tap opens the speed menu, the current one has a check mark
@Composable
private fun SpeedChip(speed: Float, enabled: Boolean, onSelect: (Float) -> Unit) {
    val colors = MaterialTheme.colorScheme
    val label = speedLabel(speed)
    val digits = MaterialTheme.typography.labelLarge.copy(fontFeatureSettings = "tnum")
    var open by remember { mutableStateOf(false) }
    Box {
        Surface(
            onClick = { open = true },
            enabled = enabled,
            shape = CircleShape,
            color = colors.surface,
            contentColor = if (enabled) colors.onSurface else colors.onSurface.copy(alpha = DISABLED_ALPHA),
            modifier = Modifier.semantics {
                contentDescription = SPEED
                stateDescription = label
            },
        ) {
            Box(Modifier.defaultMinSize(minWidth = SPEED_WIDTH, minHeight = CHIP_HEIGHT), contentAlignment = Alignment.Center) {
                Text(label, style = digits)
            }
        }
        DropdownMenuPopup(expanded = open, onDismissRequest = { open = false }) {
            DropdownMenuGroup(shapes = MenuDefaults.groupShape(0, 1)) {
                SPEEDS.forEachIndexed { index, option ->
                    SelectableDropdownMenuItem(
                        selected = option == speed,
                        shapes = MenuDefaults.itemShape(index, SPEEDS.size),
                        text = { Text(speedLabel(option), style = digits) },
                        onClick = {
                            open = false
                            onSelect(option)
                        },
                        leadingIcon = { Spacer(Modifier.size(MenuDefaults.LeadingIconSize)) },
                        selectedLeadingIcon = { Icon(Icons.Rounded.Check, contentDescription = null) },
                    )
                }
            }
        }
    }
}

private fun fraction(positionMs: Long, durationMs: Long): Float =
    if (durationMs <= 0) 0f else (positionMs.toFloat() / durationMs).coerceIn(0f, 1f)

private val SEEK_HEIGHT = 40.dp
private val SKIP_ICON_SIZE = 28.dp
private val SKIP_DIGITS_SIZE = 9.dp
private val SPEED_WIDTH = 64.dp
private val CHIP_HEIGHT = 32.dp

// the ring center of the Replay icon sits 1/24 of the height below the middle
private const val RING_CENTER_SHIFT = 24
private const val TRACK_ALPHA = 0.24f
private const val OUTLINE_ALPHA = 0.5f
private const val DISABLED_ALPHA = 0.38f
