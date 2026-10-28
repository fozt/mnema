package dev.mnema.app.ui.settings

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.relocation.BringIntoViewRequester
import androidx.compose.foundation.relocation.bringIntoViewRequester
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.Notes
import androidx.compose.material.icons.rounded.BatteryChargingFull
import androidx.compose.material.icons.rounded.Check
import androidx.compose.material.icons.rounded.DarkMode
import androidx.compose.material.icons.rounded.ExpandLess
import androidx.compose.material.icons.rounded.ExpandMore
import androidx.compose.material.icons.rounded.Folder
import androidx.compose.material.icons.rounded.GraphicEq
import androidx.compose.material.icons.rounded.Notifications
import androidx.compose.material.icons.rounded.Palette
import androidx.compose.material.icons.rounded.Translate
import androidx.compose.material.icons.rounded.UnfoldMore
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.ui.platform.LocalWindowInfo
import androidx.compose.ui.unit.DpOffset
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.mnema.app.AppGraph
import dev.mnema.app.i18n.tr
import dev.mnema.app.model.Lang
import dev.mnema.app.settings.ColorSource
import dev.mnema.app.settings.Settings
import dev.mnema.app.settings.SummaryLang
import dev.mnema.app.settings.ThemeMode
import dev.mnema.app.ui.Tab
import dev.mnema.app.ui.components.MnemaTopBar
import dev.mnema.app.ui.components.SettingsGroup
import dev.mnema.app.ui.components.SettingsRow
import dev.mnema.app.ui.onboarding.rememberAccess
import dev.mnema.app.ui.onboarding.rememberAccessActions
import dev.mnema.app.ui.recordings.withSelected
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.receiveAsFlow

private val LOOK_TITLE: String get() = tr("Appearance")
private val THEME: String get() = tr("Theme")
private val COLORS: String get() = tr("Colors")
private val PROCESSING_TITLE: String get() = tr("Processing")
private val CALL_LANG: String get() = tr("Call language")
private val ASR: String get() = tr("Recognition")
private val SUMMARY_LANG: String get() = tr("Summary language")
private val ACCESS_TITLE: String get() = tr("Permissions")
private val FILES: String get() = tr("All files access")
private val NOTIFICATIONS: String get() = tr("Notifications")
private val BATTERY: String get() = tr("Unrestricted battery")
private val ALLOW: String get() = tr("Allow")
private val TURN_ON: String get() = tr("Turn on")
private val ALLOWED: String get() = tr("Allowed")
private val NOT_ALLOWED: String get() = tr("Not allowed")
private val ON: String get() = tr("On")
private val OFF: String get() = tr("Off")

// the choice menu opens under the row caption, not under the tile
private val MENU_INSET = 72.dp

/** A request to show the Processing group when Settings open: Choose server on a recording screen. */
object SettingsLinks {
    private val requests = Channel<Unit>(Channel.CONFLATED)

    internal val processing: Flow<Unit> = requests.receiveAsFlow()

    fun showProcessing() {
        requests.trySend(Unit)
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(graph: AppGraph, modifier: Modifier = Modifier) {
    val vm: SettingsViewModel = viewModel { SettingsViewModel(graph) }
    val settings by vm.settings.collectAsStateWithLifecycle()
    val scroll = TopAppBarDefaults.exitUntilCollapsedScrollBehavior()
    val processing = remember { BringIntoViewRequester() }
    val loaded = settings != null
    LaunchedEffect(loaded) {
        if (!loaded) return@LaunchedEffect
        SettingsLinks.processing.collect {
            // the group is laid out already: scroll to it after the frame
            withFrameNanos {}
            processing.bringIntoView()
        }
    }
    Scaffold(
        modifier = modifier.fillMaxSize().nestedScroll(scroll.nestedScrollConnection),
        topBar = { MnemaTopBar(Tab.SETTINGS.title, scroll) },
        contentWindowInsets = WindowInsets(0),
    ) { padding ->
        Column(
            Modifier
                .padding(padding)
                .fillMaxSize()
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 24.dp),
            verticalArrangement = Arrangement.spacedBy(24.dp),
        ) {
            settings?.let { current ->
                LookGroup(vm, current)
                ProcessingGroup(vm, current, Modifier.bringIntoViewRequester(processing))
            }
            HelperGroup(vm)
            AccessGroup()
            UpdateGroup(graph)
        }
    }
    vm.nodeDetails?.let { NodeSheet(it, onDismiss = vm::closeNode) }
}

@Composable
private fun LookGroup(vm: SettingsViewModel, settings: Settings) {
    val colors = MaterialTheme.colorScheme
    SettingsGroup(LOOK_TITLE) {
        ChoiceRow(Icons.Rounded.DarkMode, colors.tertiary, THEME, ThemeMode.entries, settings.themeMode, { it.title }) {
            vm.setTheme(it)
        }
        ChoiceRow(Icons.Rounded.Palette, colors.primary, COLORS, ColorSource.entries, settings.colorSource, { it.title }) {
            vm.setColorSource(it)
        }
    }
}

@Composable
private fun ProcessingGroup(vm: SettingsViewModel, settings: Settings, modifier: Modifier = Modifier) {
    val colors = MaterialTheme.colorScheme
    var adding by rememberSaveable { mutableStateOf(false) }
    val focused = LocalWindowInfo.current.isWindowFocused
    // back in the app: servers are checked again
    LaunchedEffect(focused) { if (focused) vm.refreshNodes() }
    val asr = withSelected(vm.asrChoices, settings.asr)
    SettingsGroup(PROCESSING_TITLE, modifier) {
        NodeRows(vm, settings, adding) { adding = it }
        ChoiceRow(Icons.Rounded.Translate, colors.secondary, CALL_LANG, Lang.entries, settings.lang, { it.title }) {
            vm.setLang(it)
        }
        ChoiceRow(Icons.Rounded.GraphicEq, colors.primary, ASR, asr, asr.first { it.id == settings.asr }, { it.title }) {
            vm.setAsr(it.id)
        }
        ChoiceRow(
            Icons.AutoMirrored.Rounded.Notes,
            colors.tertiary,
            SUMMARY_LANG,
            SummaryLang.entries,
            settings.summaryLang,
            { it.title },
        ) { vm.setSummaryLang(it) }
    }
}

@Composable
private fun AccessGroup() {
    val access = rememberAccess()
    val actions = rememberAccessActions()
    val colors = MaterialTheme.colorScheme
    SettingsGroup(ACCESS_TITLE) {
        AccessRow(Icons.Rounded.Folder, colors.secondary, FILES, access.files, ALLOWED, NOT_ALLOWED, ALLOW, actions.files)
        AccessRow(
            Icons.Rounded.Notifications,
            colors.tertiary,
            NOTIFICATIONS,
            access.notifications,
            ON,
            OFF,
            TURN_ON,
            actions.notifications,
        )
        AccessRow(
            Icons.Rounded.BatteryChargingFull,
            colors.primary,
            BATTERY,
            access.battery,
            ALLOWED,
            NOT_ALLOWED,
            ALLOW,
            actions.battery,
        )
    }
}

@Composable
private fun AccessRow(
    icon: ImageVector,
    tint: Color,
    title: String,
    done: Boolean,
    yes: String,
    no: String,
    action: String,
    onClick: () -> Unit,
) {
    SettingsRow(icon, tint, title, if (done) yes else no, onClick = if (done) null else onClick) {
        if (done) {
            Icon(Icons.Rounded.Check, contentDescription = null, tint = MaterialTheme.colorScheme.primary)
        } else {
            TextButton(onClick = onClick) { Text(action) }
        }
    }
}

/** A row with a choice from a list: the current value as its caption, the rest in a menu. */
@Composable
internal fun <T> ChoiceRow(
    icon: ImageVector,
    tint: Color,
    title: String,
    options: List<T>,
    selected: T,
    label: (T) -> String,
    onSelect: (T) -> Unit,
) {
    var open by remember { mutableStateOf(false) }
    Box {
        SettingsRow(icon, tint, title, label(selected), onClick = { open = true }) {
            Icon(Icons.Rounded.UnfoldMore, contentDescription = null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }, offset = DpOffset(MENU_INSET, 0.dp)) {
            options.forEach { option ->
                DropdownMenuItem(
                    text = { Text(label(option)) },
                    onClick = {
                        open = false
                        onSelect(option)
                    },
                    trailingIcon = if (option == selected) {
                        { Icon(Icons.Rounded.Check, contentDescription = null) }
                    } else {
                        null
                    },
                )
            }
        }
    }
}

/** A group row that expands settings fields under itself. */
@Composable
internal fun ColumnScope.ExpandRow(
    icon: ImageVector,
    tint: Color,
    title: String,
    subtitle: String?,
    expanded: Boolean,
    onToggle: () -> Unit,
    content: @Composable ColumnScope.() -> Unit,
) {
    SettingsRow(icon, tint, title, subtitle, onClick = onToggle) {
        Icon(
            if (expanded) Icons.Rounded.ExpandLess else Icons.Rounded.ExpandMore,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
    AnimatedVisibility(expanded) {
        Column(
            Modifier
                .fillMaxWidth()
                .padding(start = 16.dp, end = 16.dp, top = 4.dp, bottom = 16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
            content = content,
        )
    }
}
