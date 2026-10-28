package dev.mnema.app.ui.settings

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Add
import androidx.compose.material.icons.rounded.Dns
import androidx.compose.material.icons.rounded.MoreVert
import androidx.compose.material3.Button
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import dev.mnema.app.i18n.tr
import dev.mnema.app.server.ConnectionException
import dev.mnema.app.server.NodeCheck
import dev.mnema.app.server.parseConnection
import dev.mnema.app.settings.NodeEntry
import dev.mnema.app.settings.Settings
import dev.mnema.app.ui.components.SecretField
import dev.mnema.app.ui.components.SettingsRow

private val SERVER: String get() = tr("Server")
private val ADD_NODE: String get() = tr("Add server")
private val NO_NODES: String get() = tr("No servers")
private val CONNECTION: String get() = tr("Connection")
private val CONNECTION_HINT: String get() = tr("Invitation from the server")
private val ADD: String get() = tr("Add")
private val NODE_SETTINGS: String get() = tr("About the server")
private val REMOVE: String get() = tr("Delete")
private val MORE: String get() = tr("More")
private val NOT_SET: String get() = tr("Not set")

/** Node address without scheme and token, like Address on desktop; unset or an invalid string gives "Not set". */
fun serverSummary(connection: String): String {
    if (connection.isBlank()) return NOT_SET
    return try {
        parseConnection(connection).url.substringAfter("://")
    } catch (_: ConnectionException) {
        NOT_SET
    }
}

/** Servers by name: the chosen one processes and syncs recordings, chosen by a tap; Add server below. */
@Composable
fun ColumnScope.NodeRows(vm: SettingsViewModel, settings: Settings, adding: Boolean, onAdding: (Boolean) -> Unit) {
    Text(SERVER, Modifier.padding(horizontal = 16.dp, vertical = 8.dp), style = MaterialTheme.typography.labelLarge)
    settings.nodes.forEach { node ->
        NodeRow(
            node = node,
            active = node.address == settings.activeNode,
            check = vm.checks[node.address],
            onSelect = { vm.selectNode(node.address) },
            onSettings = { vm.openNode(node) },
            onRemove = { vm.removeNode(node.address) },
        )
    }
    val subtitle = if (settings.nodes.isEmpty()) NO_NODES else null
    ExpandRow(Icons.Rounded.Add, MaterialTheme.colorScheme.secondary, ADD_NODE, subtitle, adding, { onAdding(!adding) }) {
        ServerControls(vm)
    }
}

@Composable
private fun NodeRow(
    node: NodeEntry,
    active: Boolean,
    check: NodeCheck?,
    onSelect: () -> Unit,
    onSettings: () -> Unit,
    onRemove: () -> Unit,
) {
    val tint = if (active) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surfaceVariant
    SettingsRow(Icons.Rounded.Dns, tint, node.name, nodeStatusLabel(check), onClick = onSelect) {
        RadioButton(selected = active, onClick = null)
        NodeMenu(onSettings, onRemove)
    }
}

@Composable
private fun NodeMenu(onSettings: () -> Unit, onRemove: () -> Unit) {
    var open by remember { mutableStateOf(false) }
    Box {
        IconButton(onClick = { open = true }) { Icon(Icons.Rounded.MoreVert, contentDescription = MORE) }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            DropdownMenuItem(
                text = { Text(NODE_SETTINGS) },
                onClick = {
                    open = false
                    onSettings()
                },
            )
            DropdownMenuItem(
                text = { Text(REMOVE) },
                onClick = {
                    open = false
                    onRemove()
                },
            )
        }
    }
}

/** Add server: an invitation from the server or the address of an open server. */
@Composable
fun ServerControls(vm: SettingsViewModel) {
    val line = vm.addLine
    SecretField(
        state = vm.connection,
        label = CONNECTION,
        enabled = !vm.addBusy,
        placeholder = CONNECTION_HINT,
        supporting = line?.text,
        isError = line?.tone == Tone.ERROR,
        imeAction = ImeAction.Done,
        onDone = vm::addNode,
    )
    Button(onClick = vm::addNode, enabled = !vm.addBusy) { Text(ADD) }
}

/** The outcome of a check, save or launch as a line; an error is red. */
@Composable
internal fun LineNote(line: Line, modifier: Modifier = Modifier) {
    val colors = MaterialTheme.colorScheme
    Text(
        line.text,
        modifier,
        style = MaterialTheme.typography.bodyMedium,
        color = if (line.tone == Tone.ERROR) colors.error else colors.onSurfaceVariant,
    )
}
