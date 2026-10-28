package dev.mnema.app.ui.record

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.DeleteOutline
import androidx.compose.material.icons.rounded.Pause
import androidx.compose.material.icons.rounded.PlayArrow
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.LoadingIndicator
import androidx.compose.material3.MaterialShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.toShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.mnema.app.AppGraph
import dev.mnema.app.helper.HelperState
import dev.mnema.app.i18n.tr
import dev.mnema.app.notify.Notifications
import dev.mnema.app.notify.RecordingNotice
import dev.mnema.app.ui.components.MnemaHaptics
import dev.mnema.app.ui.components.RecordButton
import dev.mnema.app.ui.onboarding.rememberAccess
import dev.mnema.app.ui.onboarding.rememberAccessActions
import dev.mnema.app.ui.recordings.RecordingLinks
import dev.mnema.app.ui.recordings.RefreshWhileShown
import dev.mnema.app.ui.recordings.recordingSubtitle
import dev.mnema.app.ui.recordings.recordingWarning
import dev.mnema.app.ui.recordings.recordingTitle
import dev.mnema.app.ui.settings.helperActions
import dev.mnema.app.ui.theme.timer
import java.time.LocalDate
import java.time.ZoneId
import kotlinx.coroutines.delay

private val LAUNCH_TITLE: String get() = tr("Launch the helper")
private val SET_UP: String get() = tr("Set up")
private val ALLOW_FILES: String get() = tr("Allow all files access")
private val ALLOW_NOTIFICATIONS: String get() = tr("Turn on notifications")
private val RETRY: String get() = tr("Retry")
private const val TICK_MS = 1000L

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun RecordScreen(graph: AppGraph, onSetUpHelper: () -> Unit, modifier: Modifier = Modifier, onRecord: () -> Unit = {}) {
    val vm: RecordViewModel = viewModel { RecordViewModel(graph) }
    val context = LocalContext.current
    val state by vm.state.collectAsStateWithLifecycle()
    val launch by vm.launch.collectAsStateWithLifecycle()
    val last by vm.last.collectAsStateWithLifecycle()
    val sources by vm.sources.collectAsStateWithLifecycle()
    val section by vm.section.collectAsStateWithLifecycle()
    val cancelling by vm.cancelling.collectAsStateWithLifecycle()
    val cancelProblem by vm.cancelProblem.collectAsStateWithLifecycle()
    val access = rememberAccess()
    val actions = rememberAccessActions()
    val haptics = MnemaHaptics.rememberRecordHaptics()
    val running = state as? HelperState.Running
    val recording = running?.status?.recording == true
    val now by produceState(System.currentTimeMillis(), recording) {
        value = System.currentTimeMillis()
        while (recording) {
            delay(TICK_MS - value % TICK_MS)
            value = System.currentTimeMillis()
        }
    }
    val ui = recordUi(state, now, access.notifications, access.files, sources)
    // the cancel question is about the recording that was running at the tap; once it ends there is no dialog
    val writing = running?.status?.folder?.takeIf { recording && it.isNotEmpty() }
    var asked by rememberSaveable { mutableStateOf<String?>(null) }
    val zone = remember { ZoneId.systemDefault() }
    var today by remember { mutableStateOf(LocalDate.now(zone)) }
    RefreshWhileShown {
        vm.refreshRecordings()
        today = LocalDate.now(zone)
    }

    // the recording notification is checked against the helper on every change of its state
    val known = state !is HelperState.Unknown
    val notice = running?.let { RecordingNotice.decide(it.status, alive = true) }
    LaunchedEffect(known, notice) { if (known) Notifications.syncWithStatus(context) }
    // a new recording appears in the list right after Stop
    LaunchedEffect(recording) { vm.refreshRecordings() }
    // a restarted helper records both tracks until it gets the choice; a new choice gets three new attempts
    val outOfSync = sourcesOutOfSync(state, sources)
    LaunchedEffect(outOfSync, sources) { if (outOfSync) vm.syncSources() else vm.sourcesApplied() }
    LifecycleResumeEffect(vm) {
        vm.refreshPaired()
        onPauseOrDispose {}
    }

    val colors = MaterialTheme.colorScheme
    val dot = when {
        recording -> colors.error
        running?.outdated == true -> colors.tertiary
        running != null -> colors.primary
        else -> colors.outline
    }

    BoxWithConstraints(modifier.fillMaxSize()) {
        Column(
            Modifier
                .fillMaxWidth()
                .verticalScroll(rememberScrollState())
                .heightIn(min = maxHeight),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            RecordHero(heroDate(today), ui.title, ui.chip, dot, recording)
            Column(
                Modifier
                    .weight(1f)
                    .widthIn(max = CONTENT_WIDTH)
                    .fillMaxWidth()
                    .padding(horizontal = 16.dp, vertical = 24.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterVertically),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                RecordButton(
                    recording = recording,
                    enabled = ui.enabled && !launch.busy && !cancelling,
                    onClick = {
                        haptics(!recording)
                        if (!recording) onRecord()
                        vm.toggle()
                    },
                )
                AnimatedVisibility(ui.timer != null) {
                    Text(ui.timer.orEmpty(), style = MaterialTheme.typography.timer, color = colors.onSurface)
                }
                if (ui.pause != null || ui.cancel != null) {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                        ui.pause?.let { label ->
                            FilledTonalButton(onClick = vm::togglePause, enabled = !launch.busy && !cancelling) {
                                Icon(
                                    if (ui.paused) Icons.Rounded.PlayArrow else Icons.Rounded.Pause,
                                    contentDescription = null,
                                    modifier = Modifier.size(ButtonDefaults.IconSize),
                                )
                                Spacer(Modifier.size(ButtonDefaults.IconSpacing))
                                Text(label)
                            }
                        }
                        ui.cancel?.let { label ->
                            TextButton(
                                onClick = { asked = writing },
                                enabled = !launch.busy && !cancelling,
                                colors = ButtonDefaults.textButtonColors(contentColor = colors.error),
                            ) { Text(label) }
                        }
                    }
                }
                SourceToggles(ui, showLevels = recording, onToggle = vm::toggleSource, modifier = Modifier.padding(top = 8.dp))
                section?.let { SectionPicker(it, onChoose = vm::chooseSection, modifier = Modifier.padding(top = 8.dp)) }
                ui.warning?.let { Line(it, colors.error) }
                ui.warningDetail?.let { Line(it, colors.onSurfaceVariant) }
                launch.message?.let { Line(it, colors.error) }
                cancelProblem?.let { Line(it, colors.error) }
                if (launch.retry != null) TextButton(onClick = vm::retry, enabled = !launch.busy) { Text(RETRY) }
                if (!access.files && !recording) FilledTonalButton(onClick = actions.files) { Text(ALLOW_FILES) }
                if (!access.notifications) TextButton(onClick = actions.notifications) { Text(ALLOW_NOTIFICATIONS) }
                AnimatedVisibility(ui.showLaunch) {
                    LaunchCard(
                        busy = launch.busy,
                        label = helperActions(state, launch.paired, launch.result).launch,
                        onLaunch = vm::launchHelper,
                        onSetUp = onSetUpHelper,
                    )
                }
            }
            last?.let { item ->
                LastRecordingCard(
                    item = item,
                    title = recordingTitle(item, today, zone),
                    subtitle = recordingSubtitle(item, today, zone, item.section),
                    warning = recordingWarning(item),
                    onClick = { RecordingLinks.open(item.dir.path) },
                    modifier = Modifier
                        .widthIn(max = CONTENT_WIDTH)
                        .padding(start = 16.dp, end = 16.dp, bottom = 16.dp),
                )
            }
        }
    }
    asked?.takeIf { it == writing }?.let { folder ->
        CancelDialog(
            onConfirm = {
                asked = null
                vm.cancel(folder)
            },
            onDismiss = { asked = null },
        )
    }
}

@Composable
private fun CancelDialog(onConfirm: () -> Unit, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        icon = { Icon(Icons.Rounded.DeleteOutline, contentDescription = null) },
        title = { Text(CANCEL_TITLE) },
        text = { Text(CANCEL_TEXT) },
        confirmButton = {
            TextButton(onClick = onConfirm, colors = ButtonDefaults.textButtonColors(contentColor = MaterialTheme.colorScheme.error)) {
                Text(CANCEL_LABEL)
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text(KEEP_LABEL) } },
    )
}

// [label] is the launch caption; without it there is nothing to launch with, so it leads to the helper setup
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun LaunchCard(
    busy: Boolean,
    label: String?,
    onLaunch: () -> Unit,
    onSetUp: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = MaterialTheme.colorScheme
    Surface(
        modifier.fillMaxWidth(),
        shape = MaterialTheme.shapes.extraLarge,
        color = colors.secondaryContainer,
        contentColor = colors.onSecondaryContainer,
    ) {
        Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
            Row(horizontalArrangement = Arrangement.spacedBy(16.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(
                    Modifier.size(TILE_SIZE).background(colors.secondary, MaterialShapes.Cookie4Sided.toShape()),
                    contentAlignment = Alignment.Center,
                ) {
                    Icon(Icons.Rounded.PlayArrow, contentDescription = null, tint = colors.onSecondary)
                }
                Text(LAUNCH_TITLE, style = MaterialTheme.typography.titleMediumEmphasized)
            }
            when {
                busy -> LoadingIndicator(Modifier.size(48.dp).align(Alignment.CenterHorizontally))
                label != null -> Button(onClick = onLaunch, Modifier.fillMaxWidth()) { Text(label) }
                else -> Button(onClick = onSetUp, Modifier.fillMaxWidth()) { Text(SET_UP) }
            }
        }
    }
}

@Composable
private fun Line(text: String, color: Color) {
    Text(text, style = MaterialTheme.typography.bodyLarge, color = color, textAlign = TextAlign.Center)
}

private val CONTENT_WIDTH = 560.dp
private val TILE_SIZE = 40.dp
