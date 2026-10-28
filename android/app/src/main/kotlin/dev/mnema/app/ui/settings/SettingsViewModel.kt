package dev.mnema.app.ui.settings

import android.os.Build
import android.util.Log
import androidx.compose.foundation.text.input.TextFieldState
import androidx.compose.foundation.text.input.clearText
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import dev.mnema.app.AppGraph
import dev.mnema.app.helper.HelperState
import dev.mnema.app.i18n.tr
import dev.mnema.app.launch.LaunchResult
import dev.mnema.app.launch.PairingService
import dev.mnema.app.model.Lang
import dev.mnema.app.server.Health
import dev.mnema.app.server.NodeCheck
import dev.mnema.app.settings.ColorSource
import dev.mnema.app.settings.NodeEntry
import dev.mnema.app.settings.Settings
import dev.mnema.app.settings.SummaryLang
import dev.mnema.app.settings.ThemeMode
import dev.mnema.app.ui.record.helperClient
import dev.mnema.app.ui.record.launchMessage
import dev.mnema.app.ui.record.startHelper
import dev.mnema.app.ui.recordings.fallbackAsrChoices
import dev.mnema.app.ui.recordings.loadAsrChoices
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.joinAll
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.IOException

private const val LOG_TAG = "MnemaSettings"

private val CHECKING: String get() = tr("Checking…")
private val NODE_ADDED: String get() = tr("Server added")
private val SAVED: String get() = tr("Saved")
private val SAVE_FAILED: String get() = tr("Could not save")
private val BAD_PAIRING_INPUT: String get() = tr("Check the port and code")
private val FORGOTTEN: String
    get() = tr("Pairing forgotten. To revoke access, remove Mnema in “Wireless debugging”, section “Paired devices”")
private val FORGET_FAILED: String get() = tr("Could not forget the pairing")
private val COMMAND_FAILED: String get() = tr("Could not launch the helper")

enum class Tone { OK, ERROR, BUSY }

/** Result line under a field: check, save, launch. */
data class Line(val text: String, val tone: Tone)

/** Drafts of the Settings and first-launch fields; saved only after a check. */
class SettingsViewModel(private val graph: AppGraph) : ViewModel() {
    val settings: StateFlow<Settings?> = graph.settings.settings
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), null)

    val helperState: StateFlow<HelperState> =
        helperClient(graph).getOrNull()?.state ?: MutableStateFlow(HelperState.NotRunning)

    // the Add server line: cleared after adding, the token does not return to the field
    val connection = TextFieldState()

    var addLine by mutableStateOf<Line?>(null)
        private set

    /** Outcome of the last node check by address; no entry means still checking. */
    var checks by mutableStateOf<Map<String, NodeCheck>>(emptyMap())
        private set
    var nodeDetails by mutableStateOf<NodeDetails?>(null)
        private set
    var paired by mutableStateOf(graph.launcher.paired)
        private set
    var launching by mutableStateOf(false)
        private set
    var helperLine by mutableStateOf<Line?>(null)
        private set
    var lastLaunch by mutableStateOf<LaunchResult?>(null)
        private set

    /** Recognition: what the active node can do; if it did not answer, a fallback list with the saved choice. */
    var asrChoices by mutableStateOf(fallbackAsrChoices())
        private set

    private var checkedAdd: String? = null
    private var refreshing: Job? = null
    private var loadingAsr: Job? = null
    private var editing: Job? = null

    val addBusy: Boolean get() = addLine?.tone == Tone.BUSY

    init {
        viewModelScope.launch { forgetStaleAddCheck() }
        viewModelScope.launch {
            graph.settings.settings.map { it.nodes.map(NodeEntry::connection) }.distinctUntilChanged().collect { refreshNodes() }
        }
        viewModelScope.launch {
            graph.settings.settings.map { it.activeEntry()?.connection }.distinctUntilChanged().collect { refreshAsr() }
        }
        // pairing on another screen: this one may never see Paired before clearOutcome
        viewModelScope.launch { PairingService.state.collect { lastLaunch = launchAfterPairing(lastLaunch, it) } }
    }

    /** An invitation or address from the field; a refusal shows as text at the field, the input stays. */
    fun addNode() {
        if (addBusy) return
        val text = connection.text.toString().trim()
        checkedAdd = text
        addLine = Line(CHECKING, Tone.BUSY)
        viewModelScope.launch {
            val result = try {
                joinNode(text, Build.MODEL, graph::runner, graph.settings)
            } catch (e: IOException) {
                Log.w(LOG_TAG, "Node not saved", e)
                JoinResult.Refused(SAVE_FAILED)
            }
            when (result) {
                is JoinResult.Added -> {
                    // an empty field is what was checked: the Server added line stays
                    checkedAdd = ""
                    connection.clearText()
                    addLine = Line(NODE_ADDED, Tone.OK)
                }
                is JoinResult.Refused -> addLine = Line(result.text, Tone.ERROR)
            }
        }
    }

    /** All nodes are checked at once; a node that names itself differently is renamed by its answer (AppGraph.runner). */
    fun refreshNodes() {
        refreshAsr()
        refreshing?.cancel()
        refreshing = viewModelScope.launch {
            val nodes = graph.settings.current().nodes
            checks = checks.filterKeys { address -> nodes.any { it.address == address } }
            nodes.map { node ->
                launch {
                    val check = graph.runner(node)?.capabilities() ?: NodeCheck(Health.NotMnema)
                    checks = checks + (node.address to check)
                }
            }.joinAll()
        }
    }

    // a new answer replaces the old one: the list always comes from the node chosen now
    private fun refreshAsr() {
        loadingAsr?.cancel()
        loadingAsr = viewModelScope.launch {
            val current = graph.settings.current()
            asrChoices = loadAsrChoices(current.asr) { graph.runner(current)?.asrChoices().orEmpty() }
        }
    }

    /** The chosen server processes and syncs recordings; AppGraph creates the new library folder before the first sync. */
    fun selectNode(address: String) = update { it.withActive(address) }

    fun removeNode(address: String) {
        if (nodeDetails?.address == address) closeNode()
        update { it.withoutNode(address) }
    }

    fun openNode(node: NodeEntry) {
        val client = graph.runner(node) ?: return
        closeNode()
        val job = SupervisorJob(viewModelScope.coroutineContext[Job])
        editing = job
        nodeDetails = NodeDetails(node.address, node.name, client, CoroutineScope(viewModelScope.coroutineContext + job))
    }

    fun closeNode() {
        editing?.cancel()
        editing = null
        nodeDetails = null
    }

    fun setLang(lang: Lang) = update { it.copy(lang = lang) }

    fun setAsr(asr: String) = update { it.copy(asr = asr) }

    fun setTheme(mode: ThemeMode) = update { it.copy(themeMode = mode) }

    fun setColorSource(source: ColorSource) = update { it.copy(colorSource = source) }

    fun setSummaryLang(lang: SummaryLang) = update { it.copy(summaryLang = lang) }

    fun finishOnboarding(then: () -> Unit) {
        viewModelScope.launch {
            save(SAVED) { it.copy(onboardingDone = true) }
            then()
        }
    }

    fun refreshPaired() {
        paired = graph.launcher.paired
        lastLaunch = launchAfterPairing(lastLaunch, PairingService.state.value)
    }

    fun pair() {
        helperLine = null
        PairingService.start(graph.context)
    }

    fun pairManually(portText: String, code: String) {
        val port = parsePort(portText)
        if (port == null || code.isBlank()) {
            helperLine = Line(BAD_PAIRING_INPUT, Tone.ERROR)
            return
        }
        helperLine = null
        PairingService.submitManual(graph.context, port, code.trim())
    }

    fun launchHelper() {
        if (launching) return
        val client = helperClient(graph).getOrElse {
            helperLine = Line(it.message ?: COMMAND_FAILED, Tone.ERROR)
            return
        }
        launching = true
        helperLine = null
        viewModelScope.launch {
            val result = startHelper(graph, client)
            launching = false
            lastLaunch = result
            paired = graph.launcher.paired
            helperLine = launchMessage(result)?.let { Line(it, Tone.ERROR) }
        }
    }

    fun forgetPairing() {
        viewModelScope.launch {
            val forgotten = withContext(Dispatchers.IO) {
                try {
                    graph.launcher.forget()
                    true
                } catch (e: Exception) {
                    Log.w(LOG_TAG, "Pairing key not deleted", e)
                    false
                }
            }
            paired = graph.launcher.paired
            if (forgotten) lastLaunch = null
            helperLine = if (forgotten) Line(FORGOTTEN, Tone.OK) else Line(FORGET_FAILED, Tone.ERROR)
        }
    }

    // the field changed after the check: its outcome is no longer about what is in the field
    private suspend fun forgetStaleAddCheck() = snapshotFlow { connection.text.toString().trim() }.collect {
        if (it != checkedAdd && !addBusy) addLine = null
    }

    private fun update(transform: (Settings) -> Settings) {
        viewModelScope.launch { save(null, transform) }
    }

    private suspend fun save(done: String?, transform: (Settings) -> Settings): Line? = try {
        graph.settings.update(transform)
        done?.let { Line(it, Tone.OK) }
    } catch (e: IOException) {
        Log.w(LOG_TAG, "Settings not saved", e)
        Line(SAVE_FAILED, Tone.ERROR)
    }
}
