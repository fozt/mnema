package dev.mnema.app.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.LoadingIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.SheetValue
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import dev.mnema.app.i18n.tr
import kotlinx.coroutines.launch

/** Name, model and capabilities are read from the node; the client has no settings fields for them. */
@OptIn(ExperimentalMaterial3Api::class, ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun NodeSheet(details: NodeDetails, onDismiss: () -> Unit) {
    val sheet = rememberBottomSheetState(SheetValue.Hidden, setOf(SheetValue.Hidden, SheetValue.Expanded))
    val scope = rememberCoroutineScope()
    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = sheet) {
        Column(
            Modifier.fillMaxWidth().verticalScroll(rememberScrollState())
                .padding(start = 24.dp, end = 24.dp, bottom = 24.dp),
            verticalArrangement = Arrangement.spacedBy(24.dp),
        ) {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(tr("About the server"), modifier = Modifier.semantics { heading() }, style = MaterialTheme.typography.headlineSmallEmphasized)
                Text("${details.loaded?.name ?: details.title} · ${serverSummary(details.address)}", style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            val line = details.loadLine
            if (line != null) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    if (line.tone == Tone.BUSY) LoadingIndicator(Modifier.size(32.dp))
                    LineNote(line, Modifier.weight(1f))
                    if (line.tone == Tone.ERROR) OutlinedButton(onClick = details::load) { Text(tr("Retry")) }
                }
            } else details.loaded?.let { node ->
                NodeValue(tr("Server name"), node.name)
                NodeValue(tr("Model"), nodeModelSummary(node.model))
                NodeValue(tr("Recognition on the server"), if (node.engines) tr("Yes") else tr("No"))
            }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                TextButton(
                    onClick = { scope.launch { sheet.hide() }.invokeOnCompletion { if (!sheet.isVisible) onDismiss() } },
                    shapes = ButtonDefaults.shapes(),
                ) { Text(tr("Close")) }
            }
        }
    }
}

@Composable
private fun NodeValue(label: String, value: String) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(label, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary)
        Text(value, style = MaterialTheme.typography.bodyLarge)
    }
}
