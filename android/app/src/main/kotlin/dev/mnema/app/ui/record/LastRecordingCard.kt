package dev.mnema.app.ui.record

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.KeyboardArrowRight
import androidx.compose.material3.Icon
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.mnema.app.i18n.tr
import dev.mnema.app.recordings.RecordingItem
import dev.mnema.app.ui.components.StateShape
import dev.mnema.app.ui.recordings.TitleWithLength
import dev.mnema.app.ui.recordings.recordingLength

private val LAST: String get() = tr("Last recording")

/** The newest recording, except the one being recorded now. */
fun lastRecording(items: List<RecordingItem>): RecordingItem? =
    items.filterNot { it.recordingNow }.maxByOrNull { it.startedAt }

@Composable
internal fun LastRecordingCard(
    item: RecordingItem,
    title: String,
    subtitle: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    warning: String? = null,
) {
    Surface(
        onClick = onClick,
        modifier = modifier.fillMaxWidth(),
        shape = MaterialTheme.shapes.extraLarge,
        color = MaterialTheme.colorScheme.tertiaryContainer,
        contentColor = MaterialTheme.colorScheme.onTertiaryContainer,
    ) {
        Row(
            Modifier.padding(horizontal = 20.dp, vertical = 16.dp),
            horizontalArrangement = Arrangement.spacedBy(16.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            StateShape(item.state, 48.dp)
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text(LAST, style = MaterialTheme.typography.labelMedium, color = LocalContentColor.current.copy(alpha = LABEL_ALPHA))
                TitleWithLength(title, recordingLength(item), MaterialTheme.typography.titleMediumEmphasized)
                Text(subtitle, style = MaterialTheme.typography.bodyMedium, maxLines = 2, overflow = TextOverflow.Ellipsis)
                warning?.let {
                    Text(it, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.error, maxLines = 2, overflow = TextOverflow.Ellipsis)
                }
            }
            Icon(Icons.AutoMirrored.Rounded.KeyboardArrowRight, contentDescription = null)
        }
    }
}

private const val LABEL_ALPHA = 0.8f
