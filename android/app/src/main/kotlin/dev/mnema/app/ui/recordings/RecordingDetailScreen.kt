package dev.mnema.app.ui.recordings

import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import android.util.Log
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.text.TextAutoSize
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ArrowBack
import androidx.compose.material.icons.rounded.ArrowUpward
import androidx.compose.material.icons.rounded.MoreVert
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.ButtonGroup
import androidx.compose.material3.ButtonGroupDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MenuDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.toShape
import androidx.compose.foundation.clickable
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.draw.clip
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.view.WindowCompat
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.createSavedStateHandle
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.mnema.app.AppGraph
import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.i18n.tr
import dev.mnema.app.recordings.ProcessingState
import dev.mnema.app.recordings.RecordingDetail
import dev.mnema.app.settings.Settings
import dev.mnema.app.ui.components.RecordingTimestamp
import dev.mnema.app.ui.components.Bubble
import dev.mnema.app.ui.components.CardMini
import dev.mnema.app.ui.components.EmptyState
import dev.mnema.app.ui.components.FastScrollbar
import dev.mnema.app.ui.components.HeroHeader
import dev.mnema.app.ui.components.NowPlayingModel
import dev.mnema.app.ui.components.PlayingRecording
import dev.mnema.app.ui.components.TrackPlayback
import dev.mnema.app.ui.components.WavyPlayer
import dev.mnema.app.ui.components.headerCollapsed
import dev.mnema.app.ui.components.rememberCardPlayback
import dev.mnema.app.ui.components.showsToTop
import java.io.File
import java.io.IOException
import java.time.LocalDate
import java.time.ZoneId
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch

private val BACK: String get() = tr("Back")
private val SHARE: String get() = tr("Share")
private val MORE: String get() = tr("More")
private val TO_TOP: String get() = tr("To top")
private val LISTEN_HERE: String get() = tr("Listen from here")
private val MAIN_POINTS: String get() = tr("Key points")
private val REMARKS: String get() = tr("Notes")
private val CONVERSATION: String get() = tr("Conversation")
private val NO_TRANSCRIPT: String get() = tr("No transcript")
private val GONE: String get() = tr("The recording is gone")
private val SHARE_FAILED: String get() = tr("Could not share")
private const val HERO_KEY = "hero"
private const val LOG_TAG = "MnemaRecording"
private val BAR_HEIGHT = 64.dp
private val GUTTER = 16.dp

@Composable
fun RecordingDetailScreen(
    graph: AppGraph,
    nowPlaying: NowPlayingModel,
    onBack: () -> Unit,
    onOpen: (File) -> Unit,
    onChooseNode: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val model = viewModel {
        RecordingViewModel(
            graph.recordings,
            graph.processing,
            graph.recordingsRoot,
            createSavedStateHandle(),
            graph.settings.settings,
            loadAsr = { current -> graph.runner(current)?.asrChoices().orEmpty() },
        )
    }
    val state by model.state.collectAsStateWithLifecycle()
    val deleted by model.deleted.collectAsStateWithLifecycle()
    val failure by model.failure.collectAsStateWithLifecycle()
    val opened by model.opened.collectAsStateWithLifecycle()
    RefreshWhileShown(model::refresh)

    val snackbar = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    var confirmDelete by rememberSaveable { mutableStateOf(false) }
    var confirmClear by rememberSaveable { mutableStateOf(false) }
    // which action opened the processing dialog
    var sheet by rememberSaveable { mutableStateOf<DetailAction?>(null) }
    var renaming by rememberSaveable { mutableStateOf(false) }
    var moving by rememberSaveable { mutableStateOf(false) }
    val sections by model.sections.collectAsStateWithLifecycle()
    val syncing by rememberSyncing(graph)
    val trash by model.trash.collectAsStateWithLifecycle()
    LaunchedEffect(failure) {
        failure?.let {
            snackbar.showSnackbar(it)
            model.failureShown()
        }
    }
    LaunchedEffect(opened) {
        opened?.let {
            model.openedShown()
            onOpen(it)
        }
    }

    val shown = state as? DetailState.Shown
    val zone = remember { ZoneId.systemDefault() }
    val recording = shown?.detail?.item?.let { item ->
        PlayingRecording(playingKey(item), item.dir, item.id, recordingTitle(item, LocalDate.now(zone), zone))
    }
    // the player belongs to the app: the header leaves the composition when scrolled, the screen when going back, the audio does not; tracks being recorded do not play
    val player = rememberCardPlayback(
        nowPlaying,
        recording,
        shown?.detail?.let { if (it.item.recordingNow) emptyList() else it.tracks },
        lengthMs = ((shown?.detail?.item?.durationS ?: 0.0) * 1000).toLong(),
    )
    val owner = remember { Any() }
    DisposableEffect(nowPlaying, recording?.key) {
        val key = recording?.key
        key?.let { nowPlaying.showing(it, owner) }
        onDispose { key?.let { nowPlaying.hidden(it, owner) } }
    }
    LaunchedEffect(deleted) {
        if (!deleted) return@LaunchedEffect
        recording?.key?.let(nowPlaying::stop)
        onBack()
    }
    // one speed for all recordings: the one picked on one screen applies to the others
    val speed by remember { graph.settings.settings.map<Settings, Float?> { it.playbackSpeed } }.collectAsStateWithLifecycle(null)
    LaunchedEffect(player, speed) { speed?.let { player?.selectSpeed(it) } }
    val onSpeed: (Float) -> Unit = { chosen ->
        player?.selectSpeed(chosen)
        scope.launch {
            try {
                graph.settings.update { it.copy(playbackSpeed = chosen) }
            } catch (e: IOException) {
                Log.w(LOG_TAG, "Speed not saved", e)
            }
        }
    }
    val list = rememberLazyListState()
    val barBottom = WindowInsets.statusBars.getTop(LocalDensity.current) + with(LocalDensity.current) { BAR_HEIGHT.roundToPx() }
    val heroGone by remember(list, barBottom) {
        derivedStateOf {
            val header = list.layoutInfo.visibleItemsInfo.firstOrNull { it.key == HERO_KEY }
            headerCollapsed(list.firstVisibleItemIndex, header?.let { it.offset + it.size }, barBottom)
        }
    }
    // the big player has scrolled off the top: this recording's mini player is at the bottom, a tap returns to the header
    val scrolledCard = if (heroGone && recording != null && player != null) recording to player else null
    DisposableEffect(nowPlaying, scrolledCard) {
        val card = scrolledCard?.let { (shownRecording, playback) ->
            CardMini(shownRecording, playback) { scope.launch { list.animateScrollToItem(0) } }
        }
        card?.let(nowPlaying::scrolledAway)
        onDispose { card?.let(nowPlaying::scrolledBack) }
    }
    // how many items fit on the first screen: by it To top does not flicker with lines of different heights
    var firstScreen by rememberSaveable { mutableIntStateOf(0) }
    LaunchedEffect(list) {
        snapshotFlow { list.layoutInfo.visibleItemsInfo.size.takeIf { list.firstVisibleItemIndex == 0 && list.firstVisibleItemScrollOffset == 0 } }
            .collect { count -> if (count != null && count > 0) firstScreen = count }
    }
    val toTop by remember(list) {
        derivedStateOf { showsToTop(list.firstVisibleItemIndex, firstScreen.takeIf { it > 0 } ?: list.layoutInfo.visibleItemsInfo.size) }
    }
    val onAction: (DetailAction) -> Unit = { action ->
        when (action) {
            DetailAction.PROCESS, DetailAction.RETRY, DetailAction.REPROCESS -> shown?.detail?.let {
                if (opensSheet(action, it)) sheet = action else model.retry()
            }
            DetailAction.SUMMARIZE -> model.summarize()
            DetailAction.STOP -> model.stop()
            DetailAction.DELETE -> confirmDelete = true
            DetailAction.CLEAR_AUDIO -> confirmClear = true
            DetailAction.CHOOSE_NODE -> onChooseNode()
            DetailAction.RENAME, DetailAction.TITLE -> renaming = true
            DetailAction.MOVE -> moving = true
            DetailAction.SHARE_REPORT -> shown?.detail?.let { detail ->
                scope.launch { if (!shareReport(context, detail)) snackbar.showSnackbar(SHARE_FAILED) }
            }
        }
    }
    // tapping the title: a busy recording explains why it cannot be changed
    val onRename: () -> Unit = {
        val blocked = shown?.actions?.renameBlocked
        if (blocked != null) scope.launch { snackbar.showSnackbar(blocked) } else renaming = true
    }

    Scaffold(
        modifier = modifier,
        contentWindowInsets = WindowInsets(0),
        snackbarHost = { SnackbarHost(snackbar) },
        floatingActionButton = {
            if (shown != null && toTop) {
                FloatingActionButton(
                    onClick = { scope.launch { list.animateScrollToItem(0) } },
                    containerColor = MaterialTheme.colorScheme.secondaryContainer,
                    contentColor = MaterialTheme.colorScheme.onSecondaryContainer,
                ) { Icon(Icons.Rounded.ArrowUpward, contentDescription = TO_TOP) }
            }
        },
        bottomBar = {
            shown?.takeIf { it.actions.any }?.let {
                DetailButtons(it.detail, it.actions, onAction, onShareFailed = { scope.launch { snackbar.showSnackbar(SHARE_FAILED) } })
            }
        },
    ) { padding ->
        Box(Modifier.fillMaxSize()) {
            when (val current = state) {
                DetailState.Loading -> Unit
                DetailState.Gone -> Column(Modifier.fillMaxSize().statusBarsPadding()) {
                    BackButton(onBack, Modifier.padding(4.dp))
                    EmptyState(GONE, Modifier.weight(1f))
                }
                is DetailState.Shown -> DetailContent(current, player, onSpeed, list, heroGone, toTop, padding, onBack, onAction, onRename)
            }
            AnimatedVisibility(visible = shown != null && heroGone, enter = fadeIn(), exit = fadeOut()) {
                CollapsedBar(shown?.detail?.item?.let { recordingTitle(it, LocalDate.now(zone), zone) }.orEmpty(), onBack)
            }
            if (shown != null) {
                FastScrollbar(
                    list, enabled = heroGone,
                    modifier = Modifier.align(Alignment.CenterEnd).padding(
                        top = with(LocalDensity.current) { barBottom.toDp() },
                        // The bar length stays the same when To top disappears during a drag.
                        bottom = padding.calculateBottomPadding() + 88.dp,
                    ),
                )
            }
        }
    }

    if (confirmDelete && shown != null) {
        DeleteRecordingDialog(
            syncing,
            trash,
            onConfirm = { forever ->
                confirmDelete = false
                model.delete(forever)
            },
            onDismiss = { confirmDelete = false },
        )
    }
    if (renaming && shown != null) {
        RenameDialog(
            current = currentTitle(shown.detail),
            onSave = { title ->
                renaming = false
                model.rename(title)
            },
            onDismiss = { renaming = false },
        )
    }
    if (moving && shown != null) {
        MoveToSectionDialog(
            current = shown.detail.item.section.orEmpty(),
            sections = sections,
            onMove = { section, create ->
                moving = false
                model.move(section, create)
            },
            onDismiss = { moving = false },
        )
    }
    if (confirmClear && shown != null) {
        val ask = audioAsk(trash)
        DeleteDialog(ask, onConfirm = { confirmClear = false; model.clearAudio(ask.forever) }, onDismiss = { confirmClear = false })
    }
    val sheetFor = sheet
    if (sheetFor != null && shown != null) {
        ReprocessSheet(
            detail = shown.detail,
            start = sheetStart(shown.detail, shown.defaults, retrying = sheetFor == DetailAction.RETRY),
            heading = sheetAction(shown.detail).title,
            loadAsrChoices = model::asrChoices,
            loadSoundTracks = model::soundTracks,
            onDismiss = { sheet = null },
            onConfirm = { options, unchanged ->
                sheet = null
                model.process(options, unchanged, retrying = sheetFor == DetailAction.RETRY)
            },
        )
    }
}

@Composable
private fun DetailContent(
    shown: DetailState.Shown,
    player: TrackPlayback?,
    onSpeed: (Float) -> Unit,
    list: LazyListState,
    heroGone: Boolean,
    toTop: Boolean,
    padding: PaddingValues,
    onBack: () -> Unit,
    onAction: (DetailAction) -> Unit,
    onRename: () -> Unit,
) {
    val detail = shown.detail
    val item = detail.item
    val colors = MaterialTheme.colorScheme
    // header: primary in the dark theme, primaryContainer in the light one
    val hero = if (colors.surface.luminance() < 0.5f) colors.primary else colors.primaryContainer
    StatusIconsFor(if (heroGone) null else hero)

    val notes = detail.remarks.ifEmpty { listOfNotNull(problemOf(item.state)) }
    val points = detail.summary?.points.orEmpty()
    // tapping an item or a line listens from that point
    val listen = player?.let { playback ->
        { at: Double ->
            playback.seekTo((at * 1000).toLong())
            playback.toggle(true)
        }
    }
    LazyColumn(
        state = list,
        // The last line can be lifted above the floating button.
        contentPadding = PaddingValues(bottom = padding.calculateBottomPadding() + if (toTop) 88.dp else GUTTER),
        // lines of one run sit tight, sections get a top gap
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        item(key = HERO_KEY) {
            DetailHero(detail, hero, player, onSpeed, onBack, shown.actions.naming, onRename, scrolled = { list.firstVisibleItemScrollOffset })
        }
        if (notes.isNotEmpty()) {
            item(key = "remarks") {
                RemarksCard(notes, failed = item.state is ProcessingState.Failed, shown.actions.problem, onAction,
                    Modifier.padding(start = GUTTER, end = GUTTER, top = 12.dp))
            }
        }
        if (points.isNotEmpty()) {
            item(key = "points-title") { SectionTitle(MAIN_POINTS) }
            item(key = "points") {
                MainPoints(points, detail.summary?.pointTimes.orEmpty(), listen, Modifier.padding(horizontal = GUTTER))
            }
        }
        if (detail.dialog.isNotEmpty()) {
            item(key = "dialog-title") { SectionTitle(CONVERSATION) }
            itemsIndexed(detail.dialog, contentType = { _, _ -> "line" }) { index, line ->
                Bubble(line.text, mine = isMine(line), firstInRun = firstInRun(detail.dialog, index),
                    Modifier.padding(horizontal = GUTTER), at = line.at, onListen = listen, listenLabel = LISTEN_HERE)
            }
        } else if (showsNoTranscript(detail)) {
            item(key = "no-transcript") {
                Text(
                    NO_TRANSCRIPT,
                    modifier = Modifier.padding(start = GUTTER + 4.dp, end = GUTTER, top = 16.dp),
                    style = MaterialTheme.typography.bodyMedium,
                    color = colors.onSurfaceVariant,
                )
            }
        }
    }
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class, ExperimentalLayoutApi::class)
@Composable
private fun DetailHero(
    detail: RecordingDetail,
    container: Color,
    player: TrackPlayback?,
    onSpeed: (Float) -> Unit,
    onBack: () -> Unit,
    naming: DetailAction,
    onRename: () -> Unit,
    scrolled: () -> Int,
) {
    val item = detail.item
    val zone = remember { ZoneId.systemDefault() }
    val today = LocalDate.now(zone)
    HeroHeader(container = container, decoration = MaterialShapes.Cookie9Sided.toShape(), scrolled = scrolled) {
        BackButton(onBack, Modifier.offset(x = (-12).dp, y = (-4).dp))
        whenLabel(item, today, zone).takeIf { it.isNotEmpty() }?.let {
            Text(it, style = MaterialTheme.typography.labelLarge.copy(fontFeatureSettings = "tnum"))
        }
        Text(
            recordingTitle(item, today, zone),
            modifier = Modifier.clip(MaterialTheme.shapes.small).clickable(onClickLabel = naming.title, onClick = onRename),
            style = MaterialTheme.typography.headlineMediumEmphasized,
        )
        pendingLabel(item)?.let { Text(it, style = MaterialTheme.typography.labelLarge) }
        detail.summary?.about?.takeIf { it.isNotBlank() }?.let {
            SelectionContainer { Text(it, style = MaterialTheme.typography.bodyLarge) }
        }
        FlowRow(
            modifier = Modifier.padding(top = 4.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            StatusChip(item)
            recordingWarning(item)?.let { WarningChip(it) }
            item.section?.let { SectionChip(it) }
        }
        if (showsProgress(item.state) && !item.recordingNow) {
            // The wave belongs to the player; processing progress next to it looks different.
            ProcessingBar(item.state, Modifier.padding(top = 4.dp), color = LocalContentColor.current, wavy = player == null)
            (item.state as? ProcessingState.Recognizing)?.message?.takeIf { it.isNotBlank() }?.let {
                Text(it, style = MaterialTheme.typography.bodyMedium)
            }
        }
        if (player != null) WavyPlayer(player, onSpeed, Modifier.padding(top = 8.dp), lengthMs = (item.durationS * 1000).toLong())
    }
}

@Composable
private fun BackButton(onBack: () -> Unit, modifier: Modifier = Modifier) {
    IconButton(onClick = onBack, modifier = modifier) { Icon(Icons.AutoMirrored.Rounded.ArrowBack, contentDescription = BACK) }
}

// the header went under the status bar: back and the title stay on top
@Composable
private fun CollapsedBar(title: String, onBack: () -> Unit) {
    Surface(Modifier.fillMaxWidth(), color = MaterialTheme.colorScheme.surfaceContainer) {
        Row(
            Modifier
                .statusBarsPadding()
                .height(BAR_HEIGHT)
                .padding(horizontal = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            BackButton(onBack)
            Text(
                title,
                modifier = Modifier.weight(1f).padding(start = 4.dp, end = 16.dp),
                style = MaterialTheme.typography.titleLarge,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}

@Composable
private fun RemarksCard(
    notes: List<String>,
    failed: Boolean,
    action: DetailAction?,
    onAction: (DetailAction) -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = MaterialTheme.colorScheme
    Surface(
        modifier = modifier.fillMaxWidth(),
        shape = MaterialTheme.shapes.extraLarge,
        color = if (failed) colors.errorContainer else colors.tertiaryContainer,
        contentColor = if (failed) colors.onErrorContainer else colors.onTertiaryContainer,
    ) {
        Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(REMARKS, modifier = Modifier.semantics { heading() }, style = MaterialTheme.typography.titleMediumEmphasized)
            notes.forEach { Text(it, style = MaterialTheme.typography.bodyMedium) }
            action?.let { fix ->
                FilledTonalButton(onClick = { onAction(fix) }, shapes = ButtonDefaults.shapes()) { Text(fix.title) }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun MainPoints(points: List<String>, times: List<Double?>, onSeek: ((Double) -> Unit)?, modifier: Modifier = Modifier) {
    val colors = MaterialTheme.colorScheme
    val bullet = MaterialShapes.Cookie4Sided.toShape()
    Surface(modifier.fillMaxWidth(), shape = MaterialTheme.shapes.extraLarge, color = colors.surfaceContainerHigh) {
        SelectionContainer {
            Column(Modifier.padding(horizontal = 8.dp, vertical = 14.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                points.forEachIndexed { index, point ->
                    val at = times.getOrNull(index)
                    val listen = if (at != null && onSeek != null) Modifier.clickable(onClickLabel = LISTEN_HERE) { onSeek(at) } else Modifier
                    Row(
                        Modifier
                            .fillMaxWidth()
                            .clip(MaterialTheme.shapes.large)
                            .then(listen)
                            .padding(horizontal = 12.dp, vertical = 6.dp),
                        horizontalArrangement = Arrangement.spacedBy(14.dp),
                    ) {
                        Box(
                            Modifier
                                .padding(top = 5.dp)
                                .size(14.dp)
                                .background(colors.primary, bullet),
                        )
                        Column {
                            at?.let { RecordingTimestamp(it) }
                            Text(point, style = MaterialTheme.typography.bodyLarge)
                        }
                    }
                }
            }
        }
    }
}

// Share, the main action and More as one group at the bottom of the screen
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun DetailButtons(detail: RecordingDetail, actions: DetailActions, onAction: (DetailAction) -> Unit, onShareFailed: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val share = { scope.launch { if (!shareRecording(context, detail)) onShareFailed() } }
    Box(
        Modifier
            .fillMaxWidth()
            .background(MaterialTheme.colorScheme.surface)
            .padding(horizontal = GUTTER, vertical = 12.dp),
    ) {
        ButtonGroup(
            overflowIndicator = { ButtonGroupDefaults.OverflowIndicator(it) },
            modifier = Modifier.fillMaxWidth(),
        ) {
            if (actions.share) {
                customItem(
                    buttonGroupContent = {
                        val source = remember { MutableInteractionSource() }
                        Button(
                            onClick = { share() },
                            shapes = ButtonDefaults.shapes(),
                            modifier = Modifier.weight(1f).animateWidth(source),
                            interactionSource = source,
                        ) {
                            ButtonLabel(SHARE)
                        }
                    },
                    menuContent = { menu ->
                        DropdownMenuItem(
                            text = { Text(SHARE) },
                            onClick = {
                                menu.dismiss()
                                share()
                            },
                        )
                    },
                )
            }
            actions.lead?.let { lead ->
                customItem(
                    buttonGroupContent = {
                        val source = remember { MutableInteractionSource() }
                        val modifier = Modifier.weight(1f).animateWidth(source)
                        // without Share the main action is the most prominent button
                        if (!actions.share) {
                            Button(onClick = { onAction(lead) }, modifier = modifier, shapes = ButtonDefaults.shapes(), interactionSource = source) {
                                ButtonLabel(lead.title)
                            }
                        } else {
                            FilledTonalButton(
                                onClick = { onAction(lead) },
                                modifier = modifier,
                                shapes = ButtonDefaults.shapes(),
                                interactionSource = source,
                            ) {
                                ButtonLabel(lead.title)
                            }
                        }
                    },
                    menuContent = { menu ->
                        DropdownMenuItem(
                            text = { Text(lead.title) },
                            onClick = {
                                menu.dismiss()
                                onAction(lead)
                            },
                        )
                    },
                )
            }
            if (actions.more.isNotEmpty()) {
                customItem(
                    buttonGroupContent = {
                        val source = remember { MutableInteractionSource() }
                        MoreButton(actions.more, actions::blocked, onAction, source, Modifier.animateWidth(source))
                    },
                    menuContent = { menu ->
                        actions.more.forEach { action ->
                            MoreItem(action, actions.blocked(action)) {
                                menu.dismiss()
                                onAction(action)
                            }
                        }
                    },
                )
            }
        }
    }
}

// a long caption on a narrow screen shrinks instead of being cut
@Composable
private fun ButtonLabel(text: String) {
    Text(text, maxLines = 1, autoSize = TextAutoSize.StepBased(minFontSize = 11.sp, maxFontSize = 14.sp))
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun MoreButton(
    choices: List<DetailAction>,
    blocked: (DetailAction) -> String?,
    onAction: (DetailAction) -> Unit,
    source: MutableInteractionSource,
    modifier: Modifier,
) {
    var open by remember { mutableStateOf(false) }
    Box(modifier) {
        FilledTonalIconButton(
            onClick = { open = true },
            shapes = IconButtonDefaults.shapes(),
            interactionSource = source,
        ) {
            Icon(Icons.Rounded.MoreVert, contentDescription = MORE)
        }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }, shape = MaterialTheme.shapes.large) {
            choices.forEach { action ->
                MoreItem(action, blocked(action)) {
                    open = false
                    onAction(action)
                }
            }
        }
    }
}

@Composable
private fun MoreItem(action: DetailAction, blocked: String?, onClick: () -> Unit) {
    val danger = action == DetailAction.DELETE
    DropdownMenuItem(
        text = {
            Column {
                Text(action.title)
                // an unavailable item says why
                blocked?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            }
        },
        onClick = onClick,
        enabled = blocked == null,
        colors = if (danger) MenuDefaults.itemColors(textColor = MaterialTheme.colorScheme.error) else MenuDefaults.itemColors(),
    )
}

@Composable
private fun SectionTitle(text: String) {
    Text(
        text,
        modifier = Modifier
            .padding(start = GUTTER + 4.dp, end = GUTTER, top = 20.dp, bottom = 8.dp)
            .semantics { heading() },
        style = MaterialTheme.typography.titleMediumEmphasized,
        color = MaterialTheme.colorScheme.primary,
    )
}

// status bar icons follow the header color while the screen is visible; once it leaves, the theme's
@Composable
private fun StatusIconsFor(background: Color?) {
    val view = LocalView.current
    val darkTheme = MaterialTheme.colorScheme.surface.luminance() < 0.5f
    val darkIcons = background?.let { it.luminance() > 0.5f } ?: !darkTheme
    LifecycleResumeEffect(view, darkIcons, darkTheme) {
        val controller = view.context.activity()?.window?.let { WindowCompat.getInsetsController(it, view) }
        controller?.isAppearanceLightStatusBars = darkIcons
        onPauseOrDispose { controller?.isAppearanceLightStatusBars = !darkTheme }
    }
}

private tailrec fun Context.activity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.activity()
    else -> null
}

private fun showsNoTranscript(detail: RecordingDetail): Boolean {
    val state = detail.item.state
    return !detail.item.recordingNow && !isProcessing(state) && state != ProcessingState.Unprocessed && state !is ProcessingState.AudioOnly
}

private fun problemOf(state: ProcessingState): String? = when (state) {
    is ProcessingState.Failed -> state.message
    is ProcessingState.NoSummary -> state.message
    is ProcessingState.AudioOnly -> state.message
    else -> null
}?.takeIf { it.isNotBlank() }
