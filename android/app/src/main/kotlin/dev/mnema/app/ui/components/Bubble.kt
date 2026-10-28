package dev.mnema.app.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.MaterialShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.toShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import dev.mnema.app.i18n.tr

private val ME: String get() = tr("Me")
private val OTHER: String get() = tr("Others")

// Others on the left, me on the right; the first line of a run gets an avatar shape and a sharp corner towards it.
// A line with a time starts [onListen] from that point on tap.
@Composable
fun Bubble(
    text: String, mine: Boolean, firstInRun: Boolean, modifier: Modifier = Modifier,
    at: Double? = null, onListen: ((Double) -> Unit)? = null, listenLabel: String? = null,
) {
    val colors = MaterialTheme.colorScheme
    Row(
        modifier
            .fillMaxWidth()
            .padding(top = if (firstInRun) RUN_GAP else 0.dp),
        horizontalArrangement = Arrangement.spacedBy(AVATAR_GAP, if (mine) Alignment.End else Alignment.Start),
        verticalAlignment = Alignment.Top,
    ) {
        if (mine) Spacer(Modifier.width(GUTTER)) else Avatar(mine = false, shown = firstInRun)
        val shape = bubbleShape(mine, firstInRun)
        val listen = if (at != null && onListen != null) {
            Modifier.clip(shape).clickable(onClickLabel = listenLabel) { onListen(at) }
        } else Modifier
        Surface(
            modifier = Modifier.weight(1f, fill = false).then(listen),
            shape = shape,
            color = if (mine) colors.primaryContainer else colors.secondaryContainer,
            contentColor = if (mine) colors.onPrimaryContainer else colors.onSecondaryContainer,
        ) {
            Column(Modifier.padding(horizontal = 16.dp, vertical = 10.dp)) {
                at?.let { RecordingTimestamp(it) }
                SelectionContainer { Text(text, style = MaterialTheme.typography.bodyLarge) }
            }
        }
        if (mine) Avatar(mine = true, shown = firstInRun) else Spacer(Modifier.width(GUTTER))
    }
}

/** Time of an item or a line; tapping the whole item listens from it. */
@Composable
fun RecordingTimestamp(at: Double, modifier: Modifier = Modifier) {
    Text(
        durationLabel(at),
        modifier = modifier.padding(bottom = 4.dp),
        style = MaterialTheme.typography.labelMedium.copy(fontFeatureSettings = "tnum"),
        color = MaterialTheme.colorScheme.primary,
    )
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun Avatar(mine: Boolean, shown: Boolean) {
    if (!shown) {
        Spacer(Modifier.size(AVATAR_SIZE))
        return
    }
    val colors = MaterialTheme.colorScheme
    val shape = if (mine) MaterialShapes.Cookie6Sided.toShape() else MaterialShapes.Cookie4Sided.toShape()
    Box(
        Modifier
            .size(AVATAR_SIZE)
            .background(if (mine) colors.primary else colors.secondary, shape)
            .semantics { contentDescription = if (mine) ME else OTHER },
    )
}

private fun bubbleShape(mine: Boolean, firstInRun: Boolean): RoundedCornerShape {
    val towardAvatar = if (firstInRun) TAIL else CORNER
    return RoundedCornerShape(
        topStart = if (mine) CORNER else towardAvatar,
        topEnd = if (mine) towardAvatar else CORNER,
        bottomEnd = CORNER,
        bottomStart = CORNER,
    )
}

private val CORNER = 20.dp
private val TAIL = 6.dp
private val GUTTER = 40.dp
private val AVATAR_SIZE = 28.dp
private val AVATAR_GAP = 8.dp
private val RUN_GAP = 8.dp
