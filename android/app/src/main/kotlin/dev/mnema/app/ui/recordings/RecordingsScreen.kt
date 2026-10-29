package dev.mnema.app.ui.recordings

import android.net.Uri
import androidx.compose.foundation.layout.Box
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.DeleteOutline
import androidx.compose.material.icons.rounded.Folder
import androidx.compose.material.icons.rounded.MoreVert
import androidx.compose.material.icons.rounded.WarningAmber
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.SwipeToDismissBox
import androidx.compose.material3.SwipeToDismissBoxState
import androidx.compose.material3.SwipeToDismissBoxValue
import androidx.compose.material3.rememberSwipeToDismissBoxState
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.material3.MenuDefaults
import androidx.compose.ui.draw.clip
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.semantics.CustomAccessibilityAction
import androidx.compose.ui.semantics.customActions
import dev.mnema.app.i18n.tr
import kotlinx.coroutines.launch
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.calculateEndPadding
import androidx.compose.foundation.layout.calculateStartPadding
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.TextButton
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.CornerSize
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.LinearWavyProgressIndicator
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.toShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.drawOutline
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.graphics.drawscope.translate
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.ui.platform.LocalLayoutDirection
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.repeatOnLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import androidx.navigation.NavGraphBuilder
import androidx.navigation.NavType
import androidx.navigation.compose.composable
import androidx.navigation.compose.navigation
import androidx.navigation.navArgument
import dev.mnema.app.AppGraph
import dev.mnema.app.recordings.ProcessingState
import dev.mnema.app.recordings.RecordingItem
import dev.mnema.app.ui.Tab
import dev.mnema.app.ui.hasTabs
import dev.mnema.app.ui.showTab
import dev.mnema.app.ui.components.EmptyState
import dev.mnema.app.ui.components.MnemaTopBar
import dev.mnema.app.ui.components.NowPlayingModel
import dev.mnema.app.ui.components.StateBadge
import dev.mnema.app.ui.components.StateShape
import dev.mnema.app.ui.components.progressOf
import dev.mnema.app.ui.settings.SettingsLinks
import java.io.File
import java.time.LocalDate
import java.time.ZoneId
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.receiveAsFlow

private val EMPTY: String get() = tr("No recordings yet")
internal val NO_SECTION: String get() = tr("No section")
private val MORE: String get() = tr("More")
private val CLEANUP: String get() = tr("Delete audio of processed recordings…")
private val NOTHING_TITLE: String get() = tr("Nothing to delete")
private val UNDERSTOOD: String get() = tr("Got it")
private val SECTION: String get() = tr("Section")
private val LIST_ROUTE = Tab.RECORDINGS.start
private const val RECORDING_ROUTE = "recordings/recording?${RecordingViewModel.FOLDER_ARG}={${RecordingViewModel.FOLDER_ARG}}"
private const val REFRESH_MS = 5_000L
private const val DECORATION_ALPHA = 0.1f
private const val DECORATION_TURN = 18f
private val SEGMENT_CORNER = 6.dp
private val DECORATION_SIZE = 200.dp

/** The Recordings tab: list and recording screen in one graph, the tab remembers the open recording. */
fun NavGraphBuilder.recordingsGraph(graph: AppGraph, navController: NavController, nowPlaying: NowPlayingModel) {
    navigation(startDestination = LIST_ROUTE, route = Tab.RECORDINGS.route) {
        composable(LIST_ROUTE) {
            RecordingsScreen(graph, onOpen = { navController.navigate(recordingRoute(it.path)) }, onDeleted = { nowPlaying.stop(playingKey(it)) })
        }
        composable(
            RECORDING_ROUTE,
            arguments = listOf(navArgument(RecordingViewModel.FOLDER_ARG) { type = NavType.StringType; defaultValue = "" }),
        ) { entry ->
            RecordingDetailScreen(
                graph,
                nowPlaying,
                onBack = { if (navController.currentBackStackEntry == entry) navController.popBackStack() },
                // a part of a recording is its own screen over the full one: Back returns to it
                onOpen = { if (navController.currentBackStackEntry == entry) navController.navigate(recordingRoute(it.path)) },
                onChooseNode = { if (navController.currentBackStackEntry == entry) openProcessingSettings(navController) },
            )
        }
    }
}

/** A recording that notifications ask to open; [OpenRequestedRecordings] navigates to it. */
object RecordingLinks {
    private val requests = Channel<String>(Channel.CONFLATED)

    internal val requested: Flow<String> = requests.receiveAsFlow()

    fun open(path: String) {
        requests.trySend(path)
    }
}

/** A recording from a link opens like a tap in Recordings: its list is underneath, Back leads to it and then to Record. */
@Composable
fun OpenRequestedRecordings(navController: NavController) {
    LaunchedEffect(navController) {
        RecordingLinks.requested.collect { path ->
            navController.currentBackStackEntryFlow.first { navController.hasTabs() }
            navController.showTab(Tab.RECORDINGS)
            navController.navigate(recordingRoute(path)) { popUpTo(LIST_ROUTE) }
        }
    }
}

// as the bottom tab does: Recordings remember the open recording, Settings show Processing
private fun openProcessingSettings(navController: NavController) {
    SettingsLinks.showProcessing()
    navController.showTab(Tab.SETTINGS)
}

private fun recordingRoute(path: String) = "recordings/recording?${RecordingViewModel.FOLDER_ARG}=${Uri.encode(path)}"

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun RecordingsScreen(graph: AppGraph, onOpen: (File) -> Unit, onDeleted: (RecordingItem) -> Unit, modifier: Modifier = Modifier) {
    val zone = remember { ZoneId.systemDefault() }
    val model = viewModel { RecordingsViewModel(graph.recordings, zone, graph.sync, graph.serverNotice) }
    val groups by model.groups.collectAsStateWithLifecycle()
    val query by model.query.collectAsStateWithLifecycle()
    val section by model.section.collectAsStateWithLifecycle()
    val sections by model.sections.collectAsStateWithLifecycle()
    val sync by model.syncStatus.collectAsStateWithLifecycle()
    val failure by model.failure.collectAsStateWithLifecycle()
    val resolving by model.resolving.collectAsStateWithLifecycle()
    val cleanup by model.cleanup.collectAsStateWithLifecycle()
    val notice by model.notice.collectAsStateWithLifecycle()
    val syncing by rememberSyncing(graph)
    val trash by model.trash.collectAsStateWithLifecycle()
    val snackbar = remember { SnackbarHostState() }
    LaunchedEffect(notice) {
        notice?.let {
            snackbar.showSnackbar(it)
            model.noticeShown()
        }
    }
    LaunchedEffect(graph) { graph.wakeSync() }
    val delete: (RecordingItem, Boolean, () -> Unit) -> Unit = { item, forever, failed ->
        model.delete(item.dir, forever, onDeleted = { onDeleted(item) }, onFailed = failed)
    }
    val move: (RecordingItem, String, Boolean) -> Unit = { item, target, create -> model.move(item.dir, target, create) }
    val today = LocalDate.now(zone)
    RefreshWhileShown(model::refresh)
    val scrollBehavior = TopAppBarDefaults.exitUntilCollapsedScrollBehavior()

    Scaffold(
        modifier = modifier.nestedScroll(scrollBehavior.nestedScrollConnection),
        topBar = { MnemaTopBar(Tab.RECORDINGS.title, scrollBehavior, actions = { LibraryMenu(model::askCleanup) }) },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        val shown = groups
        when {
            shown == null -> Unit
            else -> LazyColumn(
                contentPadding = listPadding(padding),
                verticalArrangement = Arrangement.spacedBy(2.dp),
            ) {
                item(key = "library-controls", contentType = "controls") {
                    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        // rounded like the section chips under the field
                        OutlinedTextField(value = query, onValueChange = { model.query.value = it }, modifier = Modifier.fillMaxWidth(),
                            label = { Text(tr("Search by title")) }, singleLine = true, shape = FilterChipDefaults.shape)
                        LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            item { FilterChip(selected = section == null, onClick = { model.section.value = null }, label = { Text(tr("All sections")) }) }
                            item { FilterChip(selected = section == "", onClick = { model.section.value = "" }, label = { Text(NO_SECTION) }) }
                            items(sections, key = { it }) { group ->
                                FilterChip(selected = section == group, onClick = { model.section.value = group }, label = { Text(group) })
                            }
                        }
                        failure?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyMedium) }
                        if (sync.bulkMissing.isNotEmpty()) {
                            Text(
                                tr("Recordings missing on the phone: {n}. They are kept on the server.", "n" to sync.bulkMissing.size),
                                style = MaterialTheme.typography.bodyMedium,
                            )
                            Column {
                                TextButton(onClick = { model.resolveBulk(false) }, enabled = !resolving) { Text(tr("Delete everywhere")) }
                                TextButton(onClick = { model.resolveBulk(true) }, enabled = !resolving) { Text(tr("Restore from the server")) }
                            }
                        }
                        if (sync.serverMissing.isNotEmpty()) {
                            val ask = serverMissingAsk(sync)
                            Text(ask.text, style = MaterialTheme.typography.bodyMedium)
                            Column {
                                TextButton(onClick = { model.resolveBulk(false, onServer = true) }, enabled = !resolving) { Text(ask.delete) }
                                TextButton(onClick = { model.resolveBulk(true, onServer = true) }, enabled = !resolving) { Text(tr("Restore to the server")) }
                            }
                        }
                    }
                }
                if (shown.isEmpty()) item(key = "empty") {
                    Text(if (query.isBlank() && section == null) EMPTY else tr("Nothing found"),
                        Modifier.padding(24.dp), style = MaterialTheme.typography.bodyLarge)
                }
                val ids = shown.flatMap { it.items }.mapNotNull { it.id }.groupingBy { it }.eachCount()
                fun key(item: RecordingItem): String = item.id?.takeIf { ids[it] == 1 } ?: item.dir.path
                shown.forEachIndexed { groupIndex, group ->
                    item(key = "day:${group.label}", contentType = "day") { DayHeader(group.label) }
                    // the newest recording as a large card, the rest of the day as one group under it
                    val newest = group.items.first().takeIf { groupIndex == 0 }
                    val rest = if (newest == null) group.items else group.items.drop(1)
                    if (newest != null) {
                        item(key = key(newest), contentType = "newest") {
                            RecordingActions(newest, MaterialTheme.shapes.extraLarge, syncing, trash, sections,
                                onDelete = { forever, failed -> delete(newest, forever, failed) }, onMove = move) { actions, onMenu ->
                                NewestCard(newest, today, zone, sectionTag(newest, section), onClick = { onOpen(newest.dir) }, onLongClick = onMenu, modifier = actions)
                            }
                        }
                        if (rest.isNotEmpty()) item(key = "gap", contentType = "gap") { Spacer(Modifier.height(10.dp)) }
                    }
                    itemsIndexed(rest, key = { _, item -> key(item) }, contentType = { _, _ -> "recording" }) { index, item ->
                        val shape = segmentShape(index, rest.size)
                        RecordingActions(item, shape, syncing, trash, sections,
                            onDelete = { forever, failed -> delete(item, forever, failed) }, onMove = move) { actions, onMenu ->
                            RecordingCard(item, today, zone, shape, sectionTag(item, section), onClick = { onOpen(item.dir) }, onLongClick = onMenu, modifier = actions)
                        }
                    }
                }
            }
        }
    }
    CleanupDialogs(cleanup, onConfirm = model::confirmCleanup, onDismiss = model::dismissCleanup)
}

/**
 * Card actions in the list: a right-to-left swipe opens the same dialog as Delete on the recording screen; a long press opens
 * a menu with Move to section and Delete recording. A recording being recorded or processed is neither moved nor deleted.
 */
@Composable
private fun RecordingActions(
    item: RecordingItem,
    shape: Shape,
    syncing: Boolean,
    trash: Boolean,
    sections: List<String>,
    onDelete: (forever: Boolean, onFailed: () -> Unit) -> Unit,
    onMove: (RecordingItem, String, Boolean) -> Unit,
    content: @Composable (actions: Modifier, onMenu: () -> Unit) -> Unit,
) {
    val state = rememberSwipeToDismissBoxState()
    val scope = rememberCoroutineScope()
    var asking by remember { mutableStateOf(false) }
    var menu by remember { mutableStateOf(false) }
    var moving by remember { mutableStateOf(false) }
    val deletable = canDelete(item)
    val moveBlocked = moveBlocked(item)
    val back = { scope.launch { state.reset() } }
    // the actions belong to the card itself: the screen reader focuses it, not the swipe wrapper
    val actions = Modifier.semantics {
        customActions = listOfNotNull(
            CustomAccessibilityAction(MOVE_TO_SECTION) { moving = true; true }.takeIf { moveBlocked == null },
            CustomAccessibilityAction(DetailAction.DELETE.title) { asking = true; true }.takeIf { deletable },
        )
    }
    SwipeToDismissBox(
        state = state,
        backgroundContent = { DeleteBackground(state, shape) },
        enableDismissFromStartToEnd = false,
        gesturesEnabled = deletable,
        onDismiss = { value -> if (value == SwipeToDismissBoxValue.EndToStart) asking = true else back() },
    ) {
        Box {
            content(actions) { menu = true }
            DropdownMenu(expanded = menu, onDismissRequest = { menu = false }, shape = MaterialTheme.shapes.large) {
                CardMenuItem(MOVE_TO_SECTION, moveBlocked) { menu = false; moving = true }
                CardMenuItem(DetailAction.DELETE.title, null, enabled = deletable, danger = true) { menu = false; asking = true }
            }
        }
    }
    if (moving) {
        MoveToSectionDialog(
            current = item.section.orEmpty(),
            sections = sections,
            onMove = { target, create ->
                moving = false
                onMove(item, target, create)
            },
            onDismiss = { moving = false },
        )
    }
    if (asking) {
        DeleteRecordingDialog(
            syncing,
            trash,
            onConfirm = { forever ->
                asking = false
                onDelete(forever) { back() }
            },
            onDismiss = {
                asking = false
                back()
            },
        )
    }
}

// an unavailable item says why, as in the recording screen's More menu
@Composable
private fun CardMenuItem(title: String, blocked: String?, enabled: Boolean = blocked == null, danger: Boolean = false, onClick: () -> Unit) {
    DropdownMenuItem(
        text = {
            Column {
                Text(title)
                blocked?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            }
        },
        onClick = onClick,
        enabled = enabled,
        colors = if (danger) MenuDefaults.itemColors(textColor = MaterialTheme.colorScheme.error) else MenuDefaults.itemColors(),
    )
}

@Composable
private fun DeleteBackground(state: SwipeToDismissBoxState, shape: Shape) {
    val colors = MaterialTheme.colorScheme
    if (state.dismissDirection != SwipeToDismissBoxValue.EndToStart) return
    Box(
        Modifier
            .fillMaxSize()
            .background(colors.errorContainer, shape)
            .padding(horizontal = 24.dp),
        contentAlignment = Alignment.CenterEnd,
    ) {
        Icon(Icons.Rounded.DeleteOutline, contentDescription = null, tint = colors.onErrorContainer)
    }
}

@Composable
private fun LibraryMenu(onCleanup: () -> Unit) {
    var open by remember { mutableStateOf(false) }
    Box {
        IconButton(onClick = { open = true }) { Icon(Icons.Rounded.MoreVert, contentDescription = MORE) }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }, shape = MaterialTheme.shapes.large) {
            DropdownMenuItem(text = { Text(CLEANUP) }, onClick = {
                open = false
                onCleanup()
            })
        }
    }
}

// audio goes to the trash or forever only after a confirmation with the number of recordings and the space
@Composable
private fun CleanupDialogs(dialog: CleanupDialog?, onConfirm: () -> Unit, onDismiss: () -> Unit) {
    when (dialog) {
        is CleanupDialog.Confirm -> DeleteDialog(cleanupAsk(dialog.cleanup), onConfirm, onDismiss)
        is CleanupDialog.Empty -> AlertDialog(
            onDismissRequest = onDismiss,
            title = { Text(NOTHING_TITLE) },
            text = { Text(cleanupNothing(dialog.onServer)) },
            confirmButton = { TextButton(onClick = onDismiss) { Text(UNDERSTOOD) } },
        )
        null -> Unit
    }
}

/** The list is reread while the screen is visible: the helper and processing change folders on their own. */
@Composable
internal fun RefreshWhileShown(refresh: () -> Unit) {
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    val current by rememberUpdatedState(refresh)
    LaunchedEffect(lifecycle) {
        lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
            while (true) {
                current()
                delay(REFRESH_MS)
            }
        }
    }
}

/** State chip: opaque on any colored background, an error in the error color, Not transcribed neutral. */
@Composable
internal fun StatusChip(item: RecordingItem, modifier: Modifier = Modifier) {
    val failed = item.state is ProcessingState.Failed && !item.recordingNow
    InfoChip(itemLabel(item), modifier, failed) { StateBadge(item.state, Modifier.size(20.dp)) }
}

/** A recording warning: an icon and short text on red. */
@Composable
internal fun WarningChip(text: String, modifier: Modifier = Modifier) {
    InfoChip(text, modifier, error = true) { Icon(Icons.Rounded.WarningAmber, contentDescription = null, modifier = Modifier.size(18.dp)) }
}

/** The recording's section: a folder icon and the name, display only. */
@Composable
internal fun SectionChip(name: String, modifier: Modifier = Modifier) {
    InfoChip(name, modifier) { Icon(Icons.Rounded.Folder, contentDescription = SECTION, modifier = Modifier.size(18.dp)) }
}

@Composable
internal fun InfoChip(text: String, modifier: Modifier = Modifier, error: Boolean = false, leading: (@Composable () -> Unit)? = null) {
    val colors = MaterialTheme.colorScheme
    Surface(
        modifier = modifier,
        shape = CircleShape,
        color = if (error) colors.errorContainer else colors.surface,
        contentColor = if (error) colors.onErrorContainer else colors.onSurface,
    ) {
        Row(
            Modifier.padding(start = if (leading == null) 12.dp else 6.dp, end = 12.dp, top = 6.dp, bottom = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            leading?.invoke()
            Text(text, style = MaterialTheme.typography.labelLarge, maxLines = 1)
        }
    }
}

/** Processing progress bar: while uploading and recognizing it shows how much is done, otherwise an endless wave. */
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
internal fun ProcessingBar(
    state: ProcessingState, modifier: Modifier = Modifier, color: Color = MaterialTheme.colorScheme.primary, wavy: Boolean = true,
) {
    val progress = progressOf(state)?.toFloat()
    val track = color.copy(alpha = TRACK_ALPHA)
    if (progress != null) {
        val shown by animateFloatAsState(progress, MaterialTheme.motionScheme.slowEffectsSpec(), label = "progress")
        if (wavy) {
            LinearWavyProgressIndicator(progress = { shown }, modifier = modifier.fillMaxWidth(), color = color, trackColor = track)
        } else {
            LinearProgressIndicator(progress = { shown }, modifier = modifier.fillMaxWidth(), color = color, trackColor = track)
        }
    } else {
        if (wavy) LinearWavyProgressIndicator(modifier.fillMaxWidth(), color = color, trackColor = track)
        else LinearProgressIndicator(modifier.fillMaxWidth(), color = color, trackColor = track)
    }
}

fun showsProgress(state: ProcessingState): Boolean =
    state == ProcessingState.Preparing || state is ProcessingState.Uploading || state == ProcessingState.Queued ||
        state is ProcessingState.Recognizing || state == ProcessingState.Summarizing

/** Title on the left, length on the right in tabular digits: the length never shrinks, the title gets an ellipsis. */
@Composable
internal fun TitleWithLength(title: String, length: String?, style: TextStyle, modifier: Modifier = Modifier, maxLines: Int = 1) {
    Row(modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(title, Modifier.weight(1f).alignByBaseline(), style = style, maxLines = maxLines, overflow = TextOverflow.Ellipsis)
        if (length != null) {
            Text(
                length,
                Modifier.alignByBaseline(),
                style = MaterialTheme.typography.labelLarge.copy(fontFeatureSettings = "tnum"),
                maxLines = 1,
                softWrap = false,
            )
        }
    }
}

@Composable
private fun DayHeader(label: String) {
    Text(
        label,
        modifier = Modifier
            .padding(start = 20.dp, top = 20.dp, bottom = 8.dp)
            .semantics { heading() },
        style = MaterialTheme.typography.labelLarge,
        color = MaterialTheme.colorScheme.primary,
    )
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class, ExperimentalLayoutApi::class)
@Composable
private fun NewestCard(
    item: RecordingItem, today: LocalDate, zone: ZoneId, section: String?, onClick: () -> Unit, onLongClick: () -> Unit, modifier: Modifier = Modifier,
) {
    val colors = MaterialTheme.colorScheme
    Card(
        modifier = modifier.fillMaxWidth().cardClicks(MaterialTheme.shapes.extraLarge, onClick, onLongClick),
        shape = MaterialTheme.shapes.extraLarge,
        colors = CardDefaults.cardColors(containerColor = colors.primaryContainer, contentColor = colors.onPrimaryContainer),
    ) {
        Column(
            Modifier
                .fillMaxWidth()
                .cornerDecoration(MaterialShapes.Cookie9Sided.toShape(), colors.onPrimaryContainer.copy(alpha = DECORATION_ALPHA))
                .padding(start = 24.dp, end = 24.dp, top = 24.dp, bottom = 20.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            TitleWithLength(
                recordingTitle(item, today, zone),
                recordingLength(item),
                MaterialTheme.typography.headlineSmallEmphasized,
                maxLines = 2,
            )
            if (item.about.isNotBlank()) {
                Text(item.about, style = MaterialTheme.typography.bodyLarge, maxLines = 2, overflow = TextOverflow.Ellipsis)
            }
            if (showsProgress(item.state) && !item.recordingNow) {
                ProcessingBar(item.state, Modifier.padding(vertical = 4.dp), color = LocalContentColor.current)
            }
            FlowRow(
                modifier = Modifier.padding(top = 4.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                recordingStart(item, today, zone)?.let { InfoChip(it) }
                StatusChip(item)
                recordingWarning(item)?.let { WarningChip(it) }
                pendingLabel(item)?.let { InfoChip(it) }
                section?.let { SectionChip(it) }
            }
        }
    }
}

@Composable
private fun RecordingCard(
    item: RecordingItem, today: LocalDate, zone: ZoneId, shape: Shape, section: String?, onClick: () -> Unit, onLongClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = MaterialTheme.colorScheme
    Card(
        modifier = modifier.fillMaxWidth().cardClicks(shape, onClick, onLongClick),
        shape = shape,
        colors = CardDefaults.cardColors(containerColor = colors.surfaceContainer),
    ) {
        Row(
            Modifier.padding(start = 16.dp, end = 20.dp, top = 16.dp, bottom = 16.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            StateShape(item.state, ROW_SHAPE)
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                TitleWithLength(recordingTitle(item, today, zone), recordingLength(item), MaterialTheme.typography.titleMedium)
                if (item.about.isNotBlank()) {
                    Text(
                        item.about,
                        style = MaterialTheme.typography.bodyMedium,
                        color = colors.onSurfaceVariant,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
                Text(
                    recordingSubtitle(item, today, zone, section),
                    style = MaterialTheme.typography.labelLarge,
                    color = if (item.state is ProcessingState.Failed && !item.recordingNow) colors.error else colors.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                recordingWarning(item)?.let {
                    Text(it, style = MaterialTheme.typography.labelLarge, color = colors.error, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                if (showsProgress(item.state) && !item.recordingNow) ProcessingBar(item.state, Modifier.padding(top = 8.dp))
            }
        }
    }
}

// a tap opens the recording, a long press opens the card menu
private fun Modifier.cardClicks(shape: Shape, onClick: () -> Unit, onLongClick: () -> Unit): Modifier =
    clip(shape).combinedClickable(onClick = onClick, onLongClick = onLongClick, onLongClickLabel = MORE)

// A large translucent shape runs past the card's top right edge, like on headers
private fun Modifier.cornerDecoration(shape: Shape, color: Color): Modifier = drawWithCache {
    val side = DECORATION_SIZE.toPx()
    val outline = shape.createOutline(Size(side, side), layoutDirection, this)
    onDrawBehind {
        translate(left = size.width - side * 0.6f, top = -side * 0.35f) {
            rotate(DECORATION_TURN, pivot = Offset(side / 2, side / 2)) { drawOutline(outline, color) }
        }
    }
}

// Recordings of one day are one group: outer corners on the card, small ones between recordings
@Composable
private fun segmentShape(index: Int, count: Int): Shape {
    val outer = MaterialTheme.shapes.extraLarge
    val inner = CornerSize(SEGMENT_CORNER)
    val top = if (index == 0) outer.topStart else inner
    val bottom = if (index == count - 1) outer.bottomStart else inner
    return outer.copy(topStart = top, topEnd = top, bottomEnd = bottom, bottomStart = bottom)
}

private val ROW_SHAPE = 44.dp
private const val TRACK_ALPHA = 0.24f

@Composable
internal fun listPadding(scaffold: PaddingValues, gutter: Dp = 16.dp): PaddingValues {
    val direction = LocalLayoutDirection.current
    return PaddingValues(
        start = scaffold.calculateStartPadding(direction) + gutter,
        end = scaffold.calculateEndPadding(direction) + gutter,
        top = scaffold.calculateTopPadding() + 8.dp,
        bottom = scaffold.calculateBottomPadding() + gutter,
    )
}
