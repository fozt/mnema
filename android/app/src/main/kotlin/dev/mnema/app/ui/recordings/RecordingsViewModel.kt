package dev.mnema.app.ui.recordings

import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import dev.mnema.app.i18n.RU_MONTHS_SHORT
import dev.mnema.app.i18n.Ui
import dev.mnema.app.i18n.UiLang
import dev.mnema.app.i18n.tr
import dev.mnema.app.model.AUTO_ASR
import dev.mnema.app.model.Lang
import dev.mnema.app.processing.ProcessStateFile
import dev.mnema.app.processing.ProcessingScheduler
import dev.mnema.app.processing.ReprocessOptions
import dev.mnema.app.processing.WorkScheduler
import dev.mnema.app.recordings.AudioCleanup
import dev.mnema.app.recordings.ProcessingState
import dev.mnema.app.recordings.RecordingDetail
import dev.mnema.app.recordings.RecordingItem
import dev.mnema.app.recordings.RecordingStore
import dev.mnema.app.recordings.inProgress
import dev.mnema.app.recordings.recordingDirectory
import dev.mnema.app.recordings.recordingId
import dev.mnema.app.recordings.isLibraryRecord
import dev.mnema.app.recordings.findRecording
import dev.mnema.app.sync.SyncController
import dev.mnema.app.sync.SyncStatus
import java.nio.file.Files
import java.nio.file.attribute.BasicFileAttributes
import dev.mnema.app.server.AsrOption
import dev.mnema.app.server.ConnectionException
import dev.mnema.app.server.parseConnection
import dev.mnema.app.settings.Settings
import dev.mnema.app.ui.components.durationLabel
import dev.mnema.app.ui.components.progressOf
import java.io.File
import java.io.IOException
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.util.Locale
import kotlin.math.roundToInt
import kotlin.math.roundToLong
import kotlin.coroutines.CoroutineContext
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.mapLatest
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class RecordingsViewModel(private val store: RecordingStore, private val zone: ZoneId, private val sync: SyncController? = null) : ViewModel() {
    val query = MutableStateFlow("")
    // null means all recordings, the empty string means recordings in the root without a section.
    val section = MutableStateFlow<String?>(null)
    val sections = store.sections
    val syncStatus: StateFlow<SyncStatus> = sync?.status ?: MutableStateFlow(SyncStatus())
    val resolving = MutableStateFlow(false)
    val trash: StateFlow<Boolean> = store.trashAvailable
    val failure = combine(store.failure, syncStatus) { local, remote ->
        (listOfNotNull(local) + remote.errors).distinct().joinToString("\n").ifBlank { null }
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(STOP_TIMEOUT_MS), null)
    val groups: StateFlow<List<DayGroup>?> = combine(store.items, query, section) { items, text, group ->
        groupByDay(filterRecordings(items, text, group), LocalDate.now(zone), zone)
    }.flowOn(Dispatchers.Default)
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(STOP_TIMEOUT_MS), null)

    /** Delete audio of processed recordings: first a confirmation with the number of recordings and the space. */
    val cleanup = MutableStateFlow<CleanupDialog?>(null)
    val notice = MutableStateFlow<String?>(null)
    private var cleaning = false

    fun refresh() = store.refresh()

    fun askCleanup() {
        if (cleaning) return
        cleaning = true
        viewModelScope.launch {
            try {
                cleanup.value = store.audioCleanup()?.let { if (it.records.isEmpty()) CleanupDialog.Empty(it.onServer) else CleanupDialog.Confirm(it) }
            } finally { cleaning = false }
        }
    }

    fun confirmCleanup() {
        val asked = (cleanup.value as? CleanupDialog.Confirm)?.cleanup ?: return
        cleanup.value = null
        if (cleaning) return
        cleaning = true
        viewModelScope.launch {
            try { notice.value = tr("Audio deleted from recordings: {n}", "n" to store.clearAudio(asked)) } finally { cleaning = false }
        }
    }

    fun dismissCleanup() { cleanup.value = null }

    /** A swipe in the list after confirmation; on failure the reason shows in the snackbar and the card comes back. */
    fun delete(dir: File, forever: Boolean, onDeleted: () -> Unit, onFailed: () -> Unit) {
        viewModelScope.launch {
            if (store.delete(dir, forever)) {
                onDeleted()
            } else {
                notice.value = store.failure.value ?: DELETE_FAILED
                onFailed()
            }
        }
    }

    fun noticeShown() { notice.value = null }

    /** Move to section from the card menu; on failure the reason shows in the snackbar and the recording stays. */
    fun move(dir: File, section: String, create: Boolean = false) {
        viewModelScope.launch {
            if (store.move(dir, section, create) == null) notice.value = store.failure.value ?: MOVE_FAILED
        }
    }

    /** onServer: recordings vanished on the server; delete here (forever without a trash) or restore them there. */
    fun resolveBulk(restore: Boolean, onServer: Boolean = false) {
        val controller = sync ?: return
        val status = syncStatus.value
        val ids = (if (onServer) status.serverMissing else status.bulkMissing).toList()
        if (resolving.value || ids.isEmpty()) return
        resolving.value = true
        viewModelScope.launch {
            try { controller.resolveBulk(ids, restore, forever = onServer && !restore && status.withoutTrash) }
            finally { resolving.value = false; store.refresh() }
        }
    }
}

sealed interface CleanupDialog {
    data class Confirm(val cleanup: AudioCleanup) : CleanupDialog
    data class Empty(val onServer: Boolean) : CleanupDialog
}

/** The question about recordings missing on the server and the delete-here button: without a trash only forever. */
data class ServerMissingAsk(val text: String, val delete: String)

fun serverMissingAsk(status: SyncStatus): ServerMissingAsk = if (status.withoutTrash) {
    ServerMissingAsk(tr("Deleted on another device: {n}", "n" to status.serverMissing.size), tr("Delete forever"))
} else {
    ServerMissingAsk(
        tr("Recordings missing on the server: {n}. They are kept on the phone.", "n" to status.serverMissing.size),
        tr("Delete on the phone"),
    )
}

/** Under the "Delete audio of N recordings?" question: how much space is freed. */
fun cleanupAmount(cleanup: AudioCleanup): String = tr("{size} will be freed.", "size" to sizeLabel(cleanup.bytes))

/** "350 MB", "1.2 GB": below ten with one decimal. */
fun sizeLabel(bytes: Long): String {
    val megabytes = bytes / MIB
    val (value, unit) = if (megabytes < 1024) megabytes to tr("MB") else megabytes / 1024 to tr("GB")
    val locale = if (Ui.lang == UiLang.RU) Locale.forLanguageTag("ru") else Locale.ROOT
    val number = if (value < 10) String.format(locale, "%.1f", value) else value.roundToLong().toString()
    return "$number $unit"
}

internal fun filterRecordings(items: List<RecordingItem>, query: String, section: String?): List<RecordingItem> =
    items.filter { (section == null || it.section.orEmpty() == section) && it.title.contains(query.trim(), ignoreCase = true) }

sealed interface DetailState {
    data object Loading : DetailState
    data object Gone : DetailState
    data class Shown(val detail: RecordingDetail, val actions: DetailActions, val defaults: ReprocessDefaults = ReprocessDefaults()) :
        DetailState
}

/** What the Reprocess sheet starts with: language and recognition from settings. */
data class ReprocessDefaults(val lang: Lang = Lang.AUTO, val asr: String = AUTO_ASR)

/** What the processing dialog opens with; [tracks] null means the recording's audible tracks. */
data class SheetStart(val lang: Lang, val asr: String, val title: String, val tracks: Set<String>? = null)

/** Retry opens with the failed run's choice, anything it lacks as by default; the part is the whole folder. */
fun sheetStart(detail: RecordingDetail, defaults: ReprocessDefaults, retrying: Boolean): SheetStart {
    val run = detail.failedRun.takeIf { retrying }
    val present = detail.tracks.map { it.nameWithoutExtension }.toSet()
    return SheetStart(
        lang = run?.lang ?: defaults.lang,
        asr = run?.asr ?: defaults.asr,
        title = run?.title ?: if (run?.autoName == true) "" else currentTitle(detail),
        tracks = run?.tracks?.filter { it in present }?.toSet()?.takeIf { it.isNotEmpty() },
    )
}

/** One recording's screen; the folder path is in [handle], a new one after a rename. */
class RecordingViewModel(
    private val store: RecordingStore,
    private val processing: ProcessingScheduler,
    root: File,
    private val handle: SavedStateHandle,
    settings: Flow<Settings> = flowOf(Settings()),
    private val loadAsr: suspend (Settings) -> List<AsrOption> = { fallbackAsrChoices() },
    private val io: CoroutineContext = Dispatchers.IO,
) : ViewModel() {
    private val follower = FolderFollower(root, File(handle.get<String>(FOLDER_ARG).orEmpty()), recording = handle[IDENTITY_ARG])
    private val _deleted = MutableStateFlow(false)
    private val _failure = MutableStateFlow<String?>(null)
    private val _opened = MutableStateFlow<File?>(null)
    private var lastShown: DetailState.Shown? = null
    private var currentSettings = Settings()

    val deleted: StateFlow<Boolean> = _deleted.asStateFlow()
    val failure: StateFlow<String?> = _failure.asStateFlow()
    val trash: StateFlow<Boolean> = store.trashAvailable
    val sections: StateFlow<List<String>> = store.sections

    /** A sibling folder with a part of the recording that should be opened. */
    val opened: StateFlow<File?> = _opened.asStateFlow()

    @OptIn(ExperimentalCoroutinesApi::class)
    val state: StateFlow<DetailState> = combine(store.items, settings) { _, current -> current }
        .mapLatest { load(it) }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(STOP_TIMEOUT_MS), DetailState.Loading)

    fun refresh() = store.refresh()

    fun retry() = schedule(summaryOnly = (state.value as? DetailState.Shown)?.detail?.retrySummaryOnly == true)

    /** The processing dialog's choice; Retry without changes continues the interrupted processing instead of starting anew. */
    fun process(options: ReprocessOptions, unchanged: Boolean = false, retrying: Boolean = false) {
        val detail = (state.value as? DetailState.Shown)?.detail ?: return
        if (continuesFailedRun(detail, unchanged, retrying)) schedule(summaryOnly = false) else reprocess(options)
    }

    suspend fun asrChoices(selected: String = currentSettings.asr): List<AsrOption> =
        loadAsrChoices(selected) { loadAsr(currentSettings) }

    /** Default choice of the processing dialog: all tracks with audio; the server decides whether a track is silent. */
    suspend fun soundTracks(): Set<String> =
        (state.value as? DetailState.Shown)?.detail?.tracks.orEmpty().map { it.nameWithoutExtension }.toSet()

    fun summarize() = schedule(summaryOnly = true)

    fun stop() {
        val item = (state.value as? DetailState.Shown)?.detail?.item ?: return
        if (item.recordingNow || !isProcessing(item.state)) return
        viewModelScope.launch {
            withContext(io) { processing.cancel(item.dir) }
            store.refresh()
        }
    }

    fun reprocess(options: ReprocessOptions) {
        val dir = (state.value as? DetailState.Shown)?.detail?.item?.dir ?: return
        viewModelScope.launch {
            val target = withContext(io) { processing.reprocess(dir, options) }
            when {
                target == null -> _failure.value = REPROCESS_FAILED
                target.absoluteFile != dir.absoluteFile -> _opened.value = target
            }
            store.refresh()
        }
    }

    fun openedShown() {
        _opened.value = null
    }

    // forever: "forever" was confirmed, there is no trash
    fun delete(forever: Boolean = false) {
        val shown = state.value as? DetailState.Shown ?: return
        val item = shown.detail.item
        viewModelScope.launch {
            if (store.delete(item.dir, forever)) _deleted.value = true else _failure.value = store.failure.value ?: DELETE_FAILED
        }
    }

    fun clearAudio(forever: Boolean = false) {
        val dir = (state.value as? DetailState.Shown)?.detail?.item?.dir ?: return
        viewModelScope.launch {
            if (!store.clearAudio(dir, forever)) _failure.value = store.failure.value ?: tr("Could not delete the audio")
        }
    }

    fun failureShown() {
        _failure.value = null
    }

    /** The Title dialog: empty means a name from the time; the same title changes nothing; a processing recording is named by its processing. */
    fun rename(title: String) {
        val detail = (state.value as? DetailState.Shown)?.detail ?: return
        if (renameBlocked(detail.item) != null || title.trim() == currentTitle(detail)) return
        viewModelScope.launch {
            val target = store.rename(detail.item.dir, title)
            if (target == null) _failure.value = store.failure.value ?: RENAME_FAILED else follower.moved(target)
            store.refresh()
        }
    }

    /** Move to section: the screen stays open and follows the folder; the section the recording is already in changes nothing. */
    fun move(section: String, create: Boolean = false) {
        val item = (state.value as? DetailState.Shown)?.detail?.item ?: return
        if (moveBlocked(item) != null || !create && section == item.section.orEmpty()) return
        viewModelScope.launch {
            val target = store.move(item.dir, section, create)
            if (target == null) _failure.value = store.failure.value ?: MOVE_FAILED else follower.moved(target)
            store.refresh()
        }
    }

    private fun schedule(summaryOnly: Boolean) {
        val dir = (state.value as? DetailState.Shown)?.detail?.item?.dir ?: return
        viewModelScope.launch {
            val accepted = withContext(io) { processing.enqueue(dir, summaryOnly) }
            if (!accepted) _failure.value = NOT_ACCEPTED
            store.refresh()
        }
    }

    // a recording deleted from here stays on screen while the screen leaves
    private suspend fun load(settings: Settings): DetailState {
        currentSettings = settings
        val dir = withContext(io) { follower.current() }
        val detail = dir?.let { store.detail(it) } ?: return lastShown?.takeIf { _deleted.value } ?: DetailState.Gone
        handle[FOLDER_ARG] = detail.item.dir.path
        handle[IDENTITY_ARG] = detail.item.id
        val actions = detailActions(detail, serverSet(settings))
        return DetailState.Shown(detail, actions, ReprocessDefaults(settings.lang, settings.asr)).also { lastShown = it }
    }

    companion object {
        const val FOLDER_ARG = "folder"
        private const val IDENTITY_ARG = "recording_id"
    }
}

private const val STOP_TIMEOUT_MS = 5_000L
private val DELETE_FAILED: String get() = tr("Could not delete the recording")
private val NOT_ACCEPTED: String get() = tr("The recording cannot be processed: no audio")
private val REPROCESS_FAILED: String get() = tr("Could not start processing")
private val RENAME_FAILED: String get() = tr("Could not rename the recording")
private val RENAME_AFTER: String get() = tr("Rename once recording ends")
private val MOVE_FAILED: String get() = tr("Could not move the recording")
private val MOVE_AFTER_RECORDING: String get() = tr("Move once recording ends")
private val MOVE_AFTER_PROCESSING: String get() = tr("Move once processing ends")
private const val MIB = 1024.0 * 1024

private val UNPROCESSED: String get() = tr("Not processed")
private val WAITING_NETWORK: String get() = tr("Waiting for network")
private val PREPARING: String get() = tr("Preparing")
private val UPLOADING: String get() = tr("Uploading")
private val RECOGNIZING: String get() = tr("Recognizing")
private val QUEUED: String get() = tr("Queued")
private val SUMMARIZING: String get() = tr("Summarizing")
private val DONE: String get() = tr("Done")
private val NO_SUMMARY: String get() = tr("No summary")
private val NO_SPEECH: String get() = tr("No speech")
private val AUDIO_ONLY: String get() = tr("Not transcribed")
private val FAILED: String get() = tr("Error")
private val RECORDING_NOW: String get() = tr("Recording in progress")
private val TODAY: String get() = tr("Today")
private val YESTERDAY: String get() = tr("Yesterday")

// as month_short on desktop (mnema/i18n/fmt.py)
private val EN_MONTHS_SHORT = listOf("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

private const val SEPARATOR = " · "

fun stateLabel(state: ProcessingState): String = when (state) {
    ProcessingState.Unprocessed -> UNPROCESSED
    is ProcessingState.AudioOnly -> AUDIO_ONLY
    ProcessingState.WaitingNetwork -> WAITING_NETWORK
    ProcessingState.Preparing -> PREPARING
    is ProcessingState.Uploading -> withPercent(UPLOADING, state)
    ProcessingState.Queued -> QUEUED
    is ProcessingState.Recognizing -> withPercent(RECOGNIZING, state)
    ProcessingState.Summarizing -> SUMMARIZING
    ProcessingState.Done -> DONE
    ProcessingState.NoSpeech -> NO_SPEECH
    is ProcessingState.NoSummary -> NO_SUMMARY
    is ProcessingState.Failed -> FAILED
}

fun itemLabel(item: RecordingItem): String = if (item.recordingNow) RECORDING_NOW else stateLabel(item.state)

/** A recording can be deleted unless it is being recorded or processed: on its screen and by a swipe in the list. */
fun canDelete(item: RecordingItem): Boolean = !item.recordingNow && !isProcessing(item.state)

data class DayGroup(val label: String, val items: List<RecordingItem>)

fun groupByDay(items: List<RecordingItem>, today: LocalDate, zone: ZoneId): List<DayGroup> =
    items.sortedByDescending { it.startedAt }
        .groupBy { dayOf(it.startedAt, zone) }
        .map { (day, list) -> DayGroup(dayLabel(day, today), list) }

fun dayLabel(day: LocalDate, today: LocalDate): String = when (day) {
    today -> TODAY
    today.minusDays(1) -> YESTERDAY
    else -> dateLabel(day, today)
}

// without Today and Yesterday: the caption is read later too
private fun dateLabel(day: LocalDate, today: LocalDate): String {
    val sameYear = day.year == today.year
    return when (Ui.lang) {
        UiLang.RU -> "${day.dayOfMonth} ${RU_MONTHS_SHORT[day.monthValue - 1]}" + if (sameYear) "" else " ${day.year}"
        UiLang.EN -> "${EN_MONTHS_SHORT[day.monthValue - 1]} ${day.dayOfMonth}" + if (sameYear) "" else ", ${day.year}"
    }
}

/** Recording title: its name; an untitled one gets the start day and time, "Today, 16:01". */
fun recordingTitle(item: RecordingItem, today: LocalDate, zone: ZoneId): String =
    if (item.timeTitle) timeLabel(item, dayLabel(dayOf(item.startedAt, zone), today), zone) else item.title

private fun timeLabel(item: RecordingItem, day: String, zone: ZoneId): String =
    "$day, ${hourMinute(item.startedAt, zone)}" + item.partLabel?.let { " ($it)" }.orEmpty()

/**
 * Recording caption where no day header is visible (processing dialog, share subject): "Daily · Yesterday", an untitled one
 * "Yesterday, 9:05"; [relative] false gives a numeric date: "Daily · Oct 21", "Oct 21, 9:05".
 */
fun recordingLabel(item: RecordingItem, today: LocalDate, zone: ZoneId, relative: Boolean = true): String {
    val day = dayOf(item.startedAt, zone).let { if (relative) dayLabel(it, today) else dateLabel(it, today) }
    return if (item.timeTitle) timeLabel(item, day, zone) else "${item.title}$SEPARATOR$day"
}

/** Start day and time of a titled recording, "Yesterday, 9:05"; an untitled one has them in the title, so null. */
fun recordingStart(item: RecordingItem, today: LocalDate, zone: ZoneId): String? {
    if (item.timeTitle) return null
    val day = dayLabel(dayOf(item.startedAt, zone), today)
    return "$day, ${hourMinute(item.startedAt, zone)}"
}

/**
 * Under the title: the state in lower case, for a titled one after the day and time: "Yesterday, 9:05 · recognizing · 40%";
 * [section] goes first: "Work · Yesterday, 9:05 · done".
 */
fun recordingSubtitle(item: RecordingItem, today: LocalDate, zone: ZoneId, section: String? = null): String {
    val state = if (item.recordingNow) RECORDING_NOW else stateLabel(item.state)
    return listOfNotNull(section, recordingStart(item, today, zone), state.replaceFirstChar { it.lowercaseChar() },
        pendingLabel(item)?.replaceFirstChar { it.lowercaseChar() }).joinToString(SEPARATOR)
}

/** The last processing's warning in the row and on the screen; none while the recording is being made or processed again. */
fun recordingWarning(item: RecordingItem): String? = item.warning?.takeIf { !item.recordingNow && !item.state.inProgress }

/** The custom title processing gives the folder at the end: "Will be named “Daily”" while it runs; the title is still the folder name. */
fun pendingLabel(item: RecordingItem): String? = item.pendingTitle?.let { tr("Will be named “{title}”", "title" to it) }

/** The recording's section in the All sections feed; with a section or No section chosen it is the same for all rows, so null. */
fun sectionTag(item: RecordingItem, filter: String?): String? = item.section?.takeIf { filter == null && it.isNotEmpty() }

/** Length to the right of the title: "2:41", "1:05:12"; under a second gives null. */
fun recordingLength(item: RecordingItem): String? = length(item.durationS)

/** Above the title on the screen: "Today, 14:35 · 1:05"; an untitled one has the day and time in its title already. */
fun whenLabel(item: RecordingItem, today: LocalDate, zone: ZoneId): String =
    listOfNotNull(recordingStart(item, today, zone), length(item.durationS)).joinToString(SEPARATOR)

enum class DetailAction {
    PROCESS,
    RETRY,
    REPROCESS,
    SUMMARIZE,
    STOP,
    DELETE,
    CLEAR_AUDIO,
    CHOOSE_NODE,
    RENAME,
    MOVE,
    TITLE,
    SHARE_REPORT,
    ;

    val title: String
        get() = when (this) {
            PROCESS -> tr("Process")
            RETRY -> tr("Retry")
            REPROCESS -> tr("Reprocess")
            SUMMARIZE -> tr("Summarize")
            STOP -> tr("Stop processing")
            DELETE -> tr("Delete recording")
            CLEAR_AUDIO -> tr("Delete audio")
            CHOOSE_NODE -> tr("Choose server")
            RENAME -> tr("Rename")
            MOVE -> tr("Move to section")
            TITLE -> tr("Set title…")
            SHARE_REPORT -> tr("Share recording report")
        }
}

/**
 * [lead] is the main action next to Share, [more] is the More menu, [problem] goes with a failure remark; [renameBlocked]
 * says why renaming is not possible; [naming] is what tapping the title does.
 */
data class DetailActions(
    val share: Boolean,
    val lead: DetailAction?,
    val more: List<DetailAction>,
    val problem: DetailAction? = null,
    val renameBlocked: String? = null,
    val naming: DetailAction = DetailAction.RENAME,
    val moveBlocked: String? = null,
) {
    val any: Boolean get() = share || lead != null || more.isNotEmpty()

    /** Caption of an unavailable item; null when it is available. */
    fun blocked(action: DetailAction): String? = when (action) {
        DetailAction.RENAME -> renameBlocked
        DetailAction.MOVE -> moveBlocked
        else -> null
    }
}

/** A recording being recorded or processed is not moved to a section: the item shows as unavailable with a caption. */
fun moveBlocked(item: RecordingItem): String? = when {
    item.recordingNow -> MOVE_AFTER_RECORDING
    isProcessing(item.state) -> MOVE_AFTER_PROCESSING
    else -> null
}

/** No renaming while recording: a one-line caption. A processing recording gets a title, and processing names the folder. */
fun renameBlocked(item: RecordingItem): String? = RENAME_AFTER.takeIf { item.recordingNow }

/** A processing recording gets a title that processing gives the folder at the end; others are renamed at once. */
fun namingAction(item: RecordingItem): DetailAction =
    if (!item.recordingNow && isProcessing(item.state)) DetailAction.TITLE else DetailAction.RENAME

/** Running processing is stopped first; the folder the helper is writing is left alone. */
fun detailActions(detail: RecordingDetail, serverSet: Boolean): DetailActions {
    val item = detail.item
    val sound = !item.recordingNow && item.durationS > 0 && detail.processable
    val idle = !item.recordingNow && !isProcessing(item.state)
    val choose = sheetAction(detail)
    val retry = DetailAction.RETRY.takeIf {
        item.state is ProcessingState.Failed && idle && (sound || detail.retrySummaryOnly && detail.summarizable)
    }
    val quick = retry ?: choose.takeIf {
        // without a server processing would again leave only the audio
        idle && sound && (item.state == ProcessingState.Unprocessed || item.state is ProcessingState.AudioOnly && serverSet)
    }
    val summarize = idle && detail.summarizable && (detail.summary == null || detail.retrySummaryOnly)
    val offered = listOfNotNull(
        // a full retry opens the same dialog
        choose.takeIf { idle && sound && !(retry != null && !detail.retrySummaryOnly) },
        DetailAction.SUMMARIZE.takeIf { summarize && !(retry != null && detail.retrySummaryOnly) },
        // while recording the item shows as unavailable with a caption
        namingAction(item),
        DetailAction.MOVE,
        DetailAction.CLEAR_AUDIO.takeIf { idle && detail.cleanable },
        DetailAction.SHARE_REPORT.takeIf { detail.report },
        DetailAction.DELETE.takeIf { idle },
    )
    val lead = DetailAction.STOP.takeIf { !item.recordingNow && isProcessing(item.state) }
        ?: quick ?: DetailAction.SUMMARIZE.takeIf { summarize } ?: choose.takeIf { idle && sound }
    // the node did not answer: retry is the main action, another node is chosen in Settings
    val problem = DetailAction.CHOOSE_NODE.takeIf { idle && detail.nodeDown && item.state is ProcessingState.Failed }
    return DetailActions(shareable(detail), lead, offered.filter { it != lead }, problem, renameBlocked(item), namingAction(item), moveBlocked(item))
}

/** There is one processing dialog: Process for an unprocessed recording, Reprocess for a processed one. */
fun sheetAction(detail: RecordingDetail): DetailAction = when (detail.item.state) {
    ProcessingState.Unprocessed -> DetailAction.PROCESS
    // a failure or audio only on a recording with text: it is already processed
    is ProcessingState.AudioOnly, is ProcessingState.Failed ->
        if (detail.summarizable || detail.summary != null) DetailAction.REPROCESS else DetailAction.PROCESS
    else -> DetailAction.REPROCESS
}

/** Sources are chosen for any manual processing; a summary of a ready transcript needs no dialog. */
fun opensSheet(action: DetailAction, detail: RecordingDetail): Boolean = when (action) {
    DetailAction.PROCESS, DetailAction.REPROCESS -> true
    DetailAction.RETRY -> !detail.retrySummaryOnly
    else -> false
}

/** Retrying a failure without changes in the dialog continues the same node job, whatever that run chose. */
fun continuesFailedRun(detail: RecordingDetail, unchanged: Boolean, retrying: Boolean): Boolean =
    retrying && unchanged && detail.retryContinues

/** Title in the Title and Reprocess dialogs: the one set for running processing, else the recording's own; empty if untitled. */
fun currentTitle(detail: RecordingDetail): String = detail.item.pendingTitle ?: shownTitle(detail)

/** Title from the folder name; empty if untitled. */
fun shownTitle(detail: RecordingDetail): String = if (detail.item.timeTitle) "" else detail.item.title

/** The node did not answer: Automatic and the saved choice under its id; engines, as on desktop, are known only from the node. */
fun fallbackAsrChoices(selected: String? = null): List<AsrOption> = withSelected(listOf(AsrOption(AUTO_ASR, AUTO_TITLE)), selected)

private val AUTO_TITLE: String get() = tr("Automatic")

/** A choice missing from the list stays last, captioned by its id. */
fun withSelected(choices: List<AsrOption>, selected: String?): List<AsrOption> =
    if (selected != null && choices.none { it.id == selected }) choices + AsrOption(selected, selected) else choices

/** What the node can do; no node or no answer gives [fallbackAsrChoices]. */
suspend fun loadAsrChoices(selected: String, load: suspend () -> List<AsrOption>): List<AsrOption> = try {
    withContext(Dispatchers.IO) { load() }.ifEmpty { fallbackAsrChoices(selected) }
} catch (_: IOException) { fallbackAsrChoices(selected) }

fun isProcessing(state: ProcessingState): Boolean = state.inProgress

/** The screen follows meta.id when moved between sections. */
class FolderFollower(private val root: File, start: File, recording: String? = null, snapshotKey: String? = null) {
    private var dir: File? = recordingDirectory(root, start)?.takeIf {
        val matches = (recording != null && recordingId(it) == recording) ||
            (snapshotKey != null && folderFileKey(it) == snapshotKey)
        isLibraryRecord(root, it) && ((recording == null && snapshotKey == null) || matches)
    }
    private var recordId = recording ?: dir?.let(::recordingId)
    private var inode = snapshotKey ?: dir?.let(::folderFileKey)

    /** The folder was renamed here: on a case-insensitive file system the old path still opens. */
    fun moved(to: File) {
        dir = to
        inode = folderFileKey(to)
    }

    fun current(): File? = try {
        dir?.let { known ->
            val safe = recordingDirectory(root, known)?.takeIf { isLibraryRecord(root, it) }
            if (safe != null) {
                val currentId = recordingId(safe)
                // The first publication changes the id in the same folder. A folder swapped in on the old path is another recording.
                val sameFolder = inode != null && folderFileKey(safe) == inode
                if (currentId == recordId && recordId != null || sameFolder) {
                    recordId = currentId
                    return safe
                }
            }
        }
        val moved = recordId?.let { findRecording(root, it) } ?: inode?.let { key ->
            dev.mnema.app.recordings.libraryFolders(root).records.singleOrNull { folderFileKey(it) == key }
        }
        dir = moved
        inode = moved?.let(::folderFileKey)
        moved
    } catch (_: IOException) { null }

}

internal fun folderFileKey(dir: File): String? = try {
    Files.readAttributes(dir.toPath(), BasicFileAttributes::class.java).fileKey()?.toString()
} catch (_: IOException) { null }

// a server is set as processing understands it: the connection string parses
private fun serverSet(settings: Settings): Boolean = settings.connection.isNotBlank() && try {
    parseConnection(settings.connection)
    true
} catch (_: ConnectionException) {
    false
}

private fun dayOf(epochMs: Long, zone: ZoneId): LocalDate = Instant.ofEpochMilli(epochMs).atZone(zone).toLocalDate()

// "Recognizing · 40%"; without a share just the name
private fun withPercent(label: String, state: ProcessingState): String =
    progressOf(state)?.let { tr("{label} · {percent}%", "label" to label, "percent" to (it * 100).roundToInt()) } ?: label

private fun hourMinute(epochMs: Long, zone: ZoneId): String {
    val time = Instant.ofEpochMilli(epochMs).atZone(zone).toLocalTime()
    return "${time.hour}:${pad(time.minute.toLong())}"
}

private fun length(seconds: Double): String? = if (seconds >= 1) durationLabel(seconds) else null

private fun pad(value: Long): String = value.toString().padStart(2, '0')
