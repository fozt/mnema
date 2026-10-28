package dev.mnema.app.ui.record

import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import dev.mnema.app.AppGraph
import dev.mnema.app.helper.HelperClient
import dev.mnema.app.helper.HelperCommand
import dev.mnema.app.helper.HelperState
import dev.mnema.app.helper.SourceChoice
import dev.mnema.app.helper.nextSourceCommand
import dev.mnema.app.i18n.tr
import dev.mnema.app.launch.LaunchResult
import dev.mnema.app.recordings.RecordingItem
import dev.mnema.app.settings.Settings
import dev.mnema.app.settings.sourceChoice
import dev.mnema.app.ui.recordings.NO_SECTION
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.IOException
import java.util.Locale

private const val LOG_TAG = "MnemaRecord"
private const val STOP_TIMEOUT_MS = 5_000L
private const val SOURCES_RETRY_MS = 2_000L
private const val SOURCES_ATTEMPTS = 3

private val TITLE_IDLE: String get() = tr("Recorder")
private val TITLE_READY: String get() = tr("Ready to record")
private val TITLE_CALL: String get() = tr("Call in progress")
private val TITLE_RECORDING: String get() = tr("Recording in progress")
private val TITLE_PAUSED: String get() = tr("Paused")
private val PAUSE: String get() = tr("Pause")
private val RESUME: String get() = tr("Resume")
private val BUTTON_RECORD: String get() = tr("Start recording")
private val BUTTON_STOP: String get() = tr("Stop recording")
private val HELPER_UNKNOWN: String get() = tr("Checking the helper…")
private val HELPER_NOT_RUNNING: String get() = tr("Helper is not running")
private val HELPER_OUTDATED: String get() = tr("Helper is outdated, restart it")
private val HELPER_RUNNING: String get() = tr("Helper is running")
private val CHIP_OUTDATED: String get() = tr("Helper is outdated")
private val MIC_SILENCED: String get() = tr("Android muted the microphone")
private val SOURCES_OFF: String get() = tr("Both sources are off")
private val NO_FILES: String get() = tr("All files access is not allowed")
private val NO_NOTIFICATIONS: String get() = tr("Notifications are off")
private val NOT_PAIRED: String get() = tr("Pair the phone first")
private val NO_WIFI: String get() = tr("Launching requires Wi-Fi")
private val START_FAILED: String get() = tr("Could not launch the helper")
internal val SEND_FAILED: String get() = tr("Could not send the command to the helper")
private val SOURCES_NOT_APPLIED: String get() = tr("The helper did not switch sources")

data class RecordUi(
    val title: String,
    val chip: String?,
    val button: String,
    val enabled: Boolean,
    val timer: String?,
    val callLevel: Float,
    val micLevel: Float,
    val warning: String?,
    val showLaunch: Boolean,
    val callOn: Boolean,
    val micOn: Boolean,
    val paused: Boolean = false,
    val pause: String? = null,
    val cancel: String? = null,
    // what the running recording's track carries; null shows nothing under the chip
    val callSound: TrackSound? = null,
    val micSound: TrackSound? = null,
    // line under the warning: what caused it
    val warningDetail: String? = null,
)

enum class Source { CALL, MIC }

/** The Record tab from the helper heartbeat; [nowMs] is wall time, like `started_ms`; [choice] is the track choice from settings. */
fun recordUi(
    state: HelperState,
    nowMs: Long,
    notificationsAllowed: Boolean,
    filesAllowed: Boolean = true,
    choice: SourceChoice? = null,
): RecordUi {
    val running = state as? HelperState.Running
    val status = running?.status
    val recording = status?.recording == true
    val paused = recording && status.paused == true
    val outdated = running?.outdated == true
    val callOn = choice?.call ?: status?.callOn ?: true
    val micOn = choice?.mic ?: status?.micOn ?: true
    val callSound = trackSound(status, Source.CALL)?.takeIf { callOn }
    val warning = when {
        callSound == TrackSound.BLOCKED -> CALL_BLOCKED
        recording && micOn && status.micSilenced == true -> MIC_SILENCED
        !callOn && !micOn -> SOURCES_OFF
        status != null && !recording && status.error.isNotBlank() -> status.error
        !filesAllowed && !recording -> NO_FILES
        !notificationsAllowed -> NO_NOTIFICATIONS
        else -> null
    }
    val ready = running != null && filesAllowed
    return RecordUi(
        title = when {
            paused -> TITLE_PAUSED
            recording -> TITLE_RECORDING
            status?.callActive == true -> TITLE_CALL
            state is HelperState.NotRunning -> HELPER_NOT_RUNNING
            ready -> TITLE_READY
            else -> TITLE_IDLE
        },
        chip = helperChip(state),
        button = if (recording) BUTTON_STOP else BUTTON_RECORD,
        enabled = running != null && (recording || filesAllowed),
        timer = if (recording) recordingClock(status.recordedMs(nowMs)) else null,
        callLevel = if (recording && callOn && !paused) level(status.callRms) else 0f,
        micLevel = if (recording && micOn && !paused) level(status.micRms) else 0f,
        warning = warning,
        showLaunch = state is HelperState.NotRunning || (outdated && !recording),
        callOn = callOn,
        micOn = micOn,
        paused = paused,
        pause = when (pauseCommand(state)) {
            HelperCommand.PAUSE -> PAUSE
            HelperCommand.RESUME -> RESUME
            else -> null
        },
        cancel = if (recording && status.folder.isNotEmpty()) CANCEL_LABEL else null,
        callSound = callSound,
        micSound = trackSound(status, Source.MIC)?.takeIf { micOn },
        warningDetail = CALL_BLOCKED_HINT.takeIf { callSound == TrackSound.BLOCKED },
    )
}

/** Section on Record: "No section" is the empty string, then the library sections; [selected] is where the recording goes. */
data class SectionPick(val options: List<String>, val selected: String)

fun sectionTitle(name: String): String = name.ifEmpty { NO_SECTION }

/** No sections means nothing to pick; a vanished choice leaves the recording without a section, and that is what is shown. */
fun sectionPick(sections: List<String>, chosen: String): SectionPick? =
    if (sections.isEmpty()) null else SectionPick(listOf("") + sections, chosen.takeIf { it in sections }.orEmpty())

/** What the pause button sends; null when nothing is recording. */
fun pauseCommand(state: HelperState): HelperCommand? {
    val status = (state as? HelperState.Running)?.status?.takeIf { it.recording } ?: return null
    return when (status.paused) {
        true -> HelperCommand.RESUME
        false -> HelperCommand.PAUSE
    }
}

/** Time to send the choice: the helper is current, reports its tracks and records something else than chosen. */
fun sourcesOutOfSync(state: HelperState, choice: SourceChoice?): Boolean {
    val running = state as? HelperState.Running ?: return false
    return choice != null && !running.outdated && nextSourceCommand(running.status, choice) != null
}

fun Settings.toggled(source: Source): Settings = when (source) {
    Source.CALL -> copy(recordCall = !recordCall)
    Source.MIC -> copy(recordMic = !recordMic)
}

fun helperLine(state: HelperState): String = when (state) {
    HelperState.Unknown -> HELPER_UNKNOWN
    HelperState.NotRunning -> HELPER_NOT_RUNNING
    is HelperState.Running -> if (state.outdated) HELPER_OUTDATED else HELPER_RUNNING
}

// not running: the header title already says so
fun helperChip(state: HelperState): String? = when (state) {
    HelperState.Unknown -> HELPER_UNKNOWN
    HelperState.NotRunning -> null
    is HelperState.Running -> if (state.outdated) CHIP_OUTDATED else HELPER_RUNNING
}

enum class RecordAction { NONE, START, STOP, RELAUNCH_AND_START }

fun recordAction(state: HelperState): RecordAction {
    val running = state as? HelperState.Running ?: return RecordAction.NONE
    return when {
        running.status.recording -> RecordAction.STOP
        running.outdated -> RecordAction.RELAUNCH_AND_START
        else -> RecordAction.START
    }
}

/** null means nothing to say: a running helper shows in its status line. */
fun launchMessage(result: LaunchResult): String? = when (result) {
    LaunchResult.Started -> null
    LaunchResult.NotPaired -> NOT_PAIRED
    LaunchResult.NoWifi -> NO_WIFI
    is LaunchResult.Failed -> result.message
}

/** The screen was opened again: the next launch rechecks a "not paired" refusal together with its message. */
fun RecordViewModel.Launch.reopened(paired: Boolean): RecordViewModel.Launch = when (result) {
    LaunchResult.NotPaired -> copy(paired = paired, result = null, message = message.takeUnless { it == NOT_PAIRED })
    else -> copy(paired = paired)
}

/** Recording timer "h:mm:ss". */
fun recordingClock(ms: Long): String {
    val total = ms.coerceAtLeast(0) / 1000
    return String.format(Locale.ROOT, "%d:%02d:%02d", total / 3600, total / 60 % 60, total % 60)
}

/** Track choice to the helper: three attempts with a pause; false when it never applied. */
internal suspend fun pushSources(helper: HelperClient, choice: () -> SourceChoice?): Boolean {
    repeat(SOURCES_ATTEMPTS) { attempt ->
        if (attempt > 0) delay(SOURCES_RETRY_MS)
        if (helper.applySources(choice = choice)) return true
    }
    return false
}

/** START only once the helper applied the track choice; false when it did not and START was not sent. IOException propagates. */
internal suspend fun startAfterSources(helper: HelperClient, choice: SourceChoice): Boolean {
    if (!helper.applySources { choice }) return false
    helper.command(HelperCommand.START, choice)
    return true
}

/** The helper client, or why there is none: shared storage may be unavailable. */
internal fun helperClient(graph: AppGraph): Result<HelperClient> = try {
    Result.success(graph.helper)
} catch (e: IllegalStateException) {
    Result.failure(e)
}

/** Starts the helper over Wireless debugging; a Kadb failure is Failed, not an exception. A new helper gets the track choice. */
internal suspend fun startHelper(graph: AppGraph, helper: HelperClient): LaunchResult = try {
    val result = graph.launcher.launch(awaitNonce = helper::awaitNonce)
    if (result == LaunchResult.Started) {
        val choice = graph.settings.current().sourceChoice()
        helper.applySources { choice }
    }
    result
} catch (e: CancellationException) {
    throw e
} catch (e: Exception) {
    Log.w(LOG_TAG, "Helper launch failed", e)
    LaunchResult.Failed(START_FAILED)
}

class RecordViewModel(private val graph: AppGraph) : ViewModel() {
    enum class Retry { START, SOURCES }

    /** [retry] is what the Retry button next to [message] repeats; [result] is the outcome of the last helper launch. */
    data class Launch(
        val busy: Boolean = false,
        val paired: Boolean = false,
        val message: String? = null,
        val retry: Retry? = null,
        val result: LaunchResult? = null,
    )

    private val client = helperClient(graph)
    val state: StateFlow<HelperState> = client.getOrNull()?.state ?: MutableStateFlow(HelperState.NotRunning)

    private val mutableLaunch = MutableStateFlow(Launch(paired = graph.launcher.paired))
    val launch: StateFlow<Launch> = mutableLaunch.asStateFlow()

    /** null until settings are read: no choice is sent to the helper before that. */
    val sources: StateFlow<SourceChoice?> = graph.settings.settings
        .map { it.sourceChoice() }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(STOP_TIMEOUT_MS), null)

    // the same sections as the chips in Recordings; the choice also applies to the running recording, which moves after it stops
    val section: StateFlow<SectionPick?> = combine(graph.settings.settings, graph.recordings.sections) { settings, sections ->
        sectionPick(sections, settings.recordSection)
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(STOP_TIMEOUT_MS), null)

    val cancelling: StateFlow<Boolean> = RecordingCancel.busy
    val cancelProblem: StateFlow<String?> = RecordingCancel.problem

    val last: StateFlow<RecordingItem?> = graph.recordings.items
        .map(::lastRecording)
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(STOP_TIMEOUT_MS), lastRecording(graph.recordings.items.value))

    fun refreshPaired() = mutableLaunch.update { it.reopened(graph.launcher.paired) }

    fun refreshRecordings() = graph.recordings.refresh()

    fun toggle() {
        val helper = client.getOrElse { return fail(it) }
        mutableLaunch.update { it.copy(message = null, retry = null) }
        RecordingCancel.dismissProblem()
        when (recordAction(state.value)) {
            RecordAction.NONE -> Unit
            RecordAction.START -> viewModelScope.launch { start(helper) }
            RecordAction.STOP -> viewModelScope.launch { send(helper, HelperCommand.STOP) }
            RecordAction.RELAUNCH_AND_START -> relaunch(helper, thenStart = true)
        }
    }

    fun retry() {
        val helper = client.getOrElse { return fail(it) }
        val what = mutableLaunch.value.retry ?: return
        mutableLaunch.update { it.copy(message = null, retry = null) }
        when {
            what == Retry.SOURCES -> viewModelScope.launch { syncSources() }
            // already recording: nothing to retry, and Stop must not be pressed by mistake
            recordAction(state.value) == RecordAction.START -> viewModelScope.launch { start(helper) }
            recordAction(state.value) == RecordAction.RELAUNCH_AND_START -> relaunch(helper, thenStart = true)
        }
    }

    fun toggleSource(source: Source) {
        viewModelScope.launch { graph.settings.update { it.toggled(source) } }
    }

    fun chooseSection(name: String) {
        viewModelScope.launch { graph.settings.update { it.copy(recordSection = name) } }
    }

    /** Sends the choice up to three times, then a message with Retry; cancelled together with the screen. */
    suspend fun syncSources() {
        val helper = client.getOrNull() ?: return
        if (pushSources(helper) { sources.value }) {
            sourcesApplied()
        } else {
            mutableLaunch.update { it.copy(message = SOURCES_NOT_APPLIED, retry = Retry.SOURCES) }
        }
    }

    /** The helper records the choice: a message about a failed sync no longer applies. */
    fun sourcesApplied() = mutableLaunch.update { if (it.retry == Retry.SOURCES) it.copy(message = null, retry = null) else it }

    fun launchHelper() {
        val helper = client.getOrElse { return fail(it) }
        relaunch(helper, thenStart = false)
    }

    /** Pause and resume: all tracks at once, as on desktop. */
    fun togglePause() {
        val helper = client.getOrElse { return fail(it) }
        val command = pauseCommand(state.value) ?: return
        mutableLaunch.update { it.copy(message = null, retry = null) }
        viewModelScope.launch { send(helper, command) }
    }

    /** Cancels recording [folder] after confirmation; runs in the app scope, not the screen's, so the deletion is not cut short. */
    fun cancel(folder: String) {
        mutableLaunch.update { it.copy(message = null, retry = null) }
        graph.scope.launch { RecordingCancel.cancel(graph, folder) }
    }

    private fun relaunch(helper: HelperClient, thenStart: Boolean) {
        if (mutableLaunch.value.busy) return
        mutableLaunch.update { it.copy(busy = true, message = null, retry = null) }
        viewModelScope.launch {
            val result = startHelper(graph, helper)
            mutableLaunch.update {
                it.copy(busy = false, paired = graph.launcher.paired, message = launchMessage(result), result = result)
            }
            // not restarted: the old helper records, the call matters more than freshness
            if (thenStart) start(helper)
        }
    }

    private suspend fun start(helper: HelperClient) {
        val choice = graph.settings.current().sourceChoice()
        val applied = withContext(Dispatchers.IO) {
            try {
                startAfterSources(helper, choice)
            } catch (e: IOException) {
                Log.w(LOG_TAG, "Command ${HelperCommand.START.wire} not sent", e)
                null
            }
        }
        when (applied) {
            null -> mutableLaunch.update { it.copy(message = SEND_FAILED) }
            false -> mutableLaunch.update { it.copy(message = SOURCES_NOT_APPLIED, retry = Retry.START) }
            true -> try { graph.recordingStarted() } catch (e: IOException) { fail(e) }
        }
    }

    private suspend fun send(helper: HelperClient, command: HelperCommand) {
        val sent = withContext(Dispatchers.IO) {
            try {
                helper.command(command)
                true
            } catch (e: IOException) {
                Log.w(LOG_TAG, "Command ${command.wire} not sent", e)
                false
            }
        }
        if (!sent) mutableLaunch.update { it.copy(message = SEND_FAILED) }
    }

    private fun fail(error: Throwable) = mutableLaunch.update { it.copy(message = error.message ?: START_FAILED) }
}
