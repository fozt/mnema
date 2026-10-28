package dev.mnema.app.ui.recordings

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.DeleteOutline
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.State
import androidx.compose.runtime.remember
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.mnema.app.AppGraph
import dev.mnema.app.i18n.plural
import dev.mnema.app.i18n.tr
import dev.mnema.app.recordings.AudioCleanup
import dev.mnema.app.settings.Settings
import kotlinx.coroutines.flow.map

private val CANCEL: String get() = tr("Cancel")
private val DELETE: String get() = tr("Delete")
private val FOREVER: String get() = tr("Delete forever")
private val TEXTS_STAY: String get() = tr("The texts will stay.")
private val TEXTS_ONLY_HERE: String get() = tr("The texts will stay only on the phone.")
private val NOTHING_ON_SERVER: String get() = tr("No processed recordings whose texts are already on the server.")
private val NOTHING_PROCESSED: String get() = tr("No processed recordings.")

/** Confirmation dialog: [forever] deletes without the trash, [danger] colors the button as an error. */
data class DeleteAsk(val title: String, val text: String, val button: String, val forever: Boolean, val danger: Boolean = forever)

/** Deleting a recording: to the trash or forever, and what happens on other devices. */
fun recordingAsk(syncing: Boolean, trash: Boolean): DeleteAsk = if (trash) {
    val text = if (syncing) tr("The recording will move to the trash and be deleted on other devices.") else tr("The recording will move to the trash.")
    DeleteAsk(tr("Delete recording?"), text, DELETE, forever = false, danger = true)
} else {
    val text = if (syncing) tr("The recording will also be deleted on other devices.") else ""
    DeleteAsk(tr("Delete recording forever?"), text, FOREVER, forever = true)
}

/** Delete audio on a recording's screen. */
fun audioAsk(trash: Boolean): DeleteAsk =
    if (trash) DeleteAsk(tr("Delete audio?"), TEXTS_STAY, DELETE, forever = false)
    else DeleteAsk(tr("Delete audio forever?"), TEXTS_STAY, FOREVER, forever = true)

/**
 * "Delete audio of processed recordings": the question says how many recordings, the text below how much space and where
 * the texts stay; the trash is as it was when counted.
 */
fun cleanupAsk(cleanup: AudioCleanup): DeleteAsk {
    val records = cleanup.records.size
    val text = "${cleanupAmount(cleanup)} ${if (cleanup.onServer) TEXTS_STAY else TEXTS_ONLY_HERE}"
    return if (cleanup.trash) DeleteAsk(plural(records, "Delete audio of {n} recording?", "Delete audio of {n} recordings?"), text, DELETE, forever = false)
    else DeleteAsk(plural(records, "Delete audio of {n} recording forever?", "Delete audio of {n} recordings forever?"), text, FOREVER, forever = true)
}

/** Nothing to delete: either nothing is processed or the texts are not on the chosen server yet. */
fun cleanupNothing(onServer: Boolean): String = if (onServer) NOTHING_ON_SERVER else NOTHING_PROCESSED

@Composable
internal fun DeleteDialog(ask: DeleteAsk, onConfirm: () -> Unit, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        icon = if (ask.danger) { { Icon(Icons.Rounded.DeleteOutline, contentDescription = null) } } else null,
        title = { Text(ask.title) },
        text = if (ask.text.isEmpty()) null else { { Text(ask.text) } },
        confirmButton = {
            TextButton(
                onClick = onConfirm,
                colors = if (ask.danger) ButtonDefaults.textButtonColors(contentColor = MaterialTheme.colorScheme.error)
                else ButtonDefaults.textButtonColors(),
            ) { Text(ask.button) }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text(CANCEL) } },
    )
}

/** The same dialog for the Delete button on a recording and for a swipe in the list; onConfirm learns whether "forever" was agreed to. */
@Composable
internal fun DeleteRecordingDialog(syncing: Boolean, trash: Boolean, onConfirm: (forever: Boolean) -> Unit, onDismiss: () -> Unit) {
    val ask = recordingAsk(syncing, trash)
    DeleteDialog(ask, onConfirm = { onConfirm(ask.forever) }, onDismiss = onDismiss)
}

/** Whether a server is chosen: recordings sync with it, and deletion reaches other devices. */
@Composable
internal fun rememberSyncing(graph: AppGraph): State<Boolean> =
    remember(graph) { graph.settings.settings.map { settings: Settings -> settings.activeEntry() != null } }
        .collectAsStateWithLifecycle(false)
