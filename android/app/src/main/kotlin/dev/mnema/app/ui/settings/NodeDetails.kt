package dev.mnema.app.ui.settings

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import dev.mnema.app.i18n.tr
import dev.mnema.app.server.NodeSettings
import dev.mnema.app.server.RunnerClient
import java.io.IOException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch

/** Read-only details of the chosen node; reloading is manual. */
class NodeDetails(
    val address: String,
    val title: String,
    private val client: RunnerClient,
    private val scope: CoroutineScope,
) {
    var loaded by mutableStateOf<NodeSettings?>(null)
        private set
    var loadLine by mutableStateOf<Line?>(null)
        private set

    init { load() }

    fun load() {
        loadLine = Line(tr("Loading…"), Tone.BUSY)
        scope.launch {
            try {
                loaded = client.nodeSettings()
                loadLine = null
            } catch (e: IOException) {
                loadLine = Line(e.message ?: tr("The server gave an unexpected answer"), Tone.ERROR)
            }
        }
    }
}
