package dev.mnema.app.ui.settings

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.input.TextFieldLineLimits
import androidx.compose.foundation.text.input.rememberTextFieldState
import androidx.compose.foundation.text.input.setTextAndPlaceCursorAtEnd
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Adb
import androidx.compose.material.icons.rounded.ExpandLess
import androidx.compose.material.icons.rounded.ExpandMore
import androidx.compose.material.icons.rounded.Link
import androidx.compose.material.icons.rounded.MoreVert
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LoadingIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.mnema.app.helper.HelperState
import dev.mnema.app.i18n.tr
import dev.mnema.app.launch.LaunchResult
import dev.mnema.app.launch.PairingService
import dev.mnema.app.launch.PairingState
import dev.mnema.app.launch.inProgress
import dev.mnema.app.ui.components.SettingsGroup
import dev.mnema.app.ui.components.SettingsRow
import dev.mnema.app.ui.onboarding.developerSettings
import dev.mnema.app.ui.onboarding.open
import dev.mnema.app.ui.onboarding.rememberAccess
import dev.mnema.app.ui.onboarding.rememberAccessActions
import dev.mnema.app.ui.record.helperLine

internal val HELPER_PURPOSE: String
    get() = tr("Records calls. Launched over Wireless debugging")

private val TITLE: String get() = tr("Helper")
private val STATE: String get() = tr("Status")
private val PAIRING_ROW: String get() = tr("Pairing")
private val PAIRED: String get() = tr("Paired")
private val NOT_PAIRED: String get() = tr("Not paired")
private val CONNECT: String get() = tr("Connect")
private val RECONNECT: String get() = tr("Reconnect")
private val MANUAL: String get() = tr("Enter manually")
private val PORT: String get() = tr("Port")
private val CODE: String get() = tr("Pairing code")
private val SPLIT_SCREEN: String get() = tr("Open Settings and Mnema in split screen")
private val NO_NOTIFICATIONS: String get() = tr("Notifications are off")
private val ALLOW_NOTIFICATIONS: String get() = tr("Turn on notifications")
private val CONNECT_MANUAL: String get() = tr("Connect with code")
private val LAUNCH: String get() = tr("Launch")
private val RELAUNCH: String get() = tr("Relaunch")
private val FORGET: String get() = tr("Forget pairing")
private val FORGET_TITLE: String get() = tr("Forget pairing?")
private val FORGET_CONFIRM: String get() = tr("Forget")
private val CANCEL: String get() = tr("Cancel")
private val MORE: String get() = tr("More")

private val SEARCHING: String get() = tr("Looking for the code window")
private val WINDOW_CLOSED: String get() = tr("The code window closed. Open it again")
private val WAITING_CODE: String get() = tr("Enter the code in the notification")
private val PAIRING: String get() = tr("Pairing…")
private val PAIRED_NOW: String get() = tr("Pairing complete")

private val STEPS: List<String>
    get() = listOf(
        tr("“Connect” opens “Developer options”"),
        tr("There tap “Wireless debugging”, then “Pair device with pairing code”"),
        tr("Enter the code in the Mnema notification without closing the code window"),
    )
private val NOTES: List<String>
    get() = listOf(
        tr("Keep “USB debugging” on"),
        tr("Turn on “Disable adb authorization timeout”"),
        tr("Launching requires Wi-Fi"),
    )

fun pairingLine(state: PairingState): String? = when (state) {
    PairingState.Idle -> null
    is PairingState.Searching -> if (state.windowClosed) WINDOW_CLOSED else SEARCHING
    is PairingState.WaitingCode -> WAITING_CODE
    PairingState.Pairing -> PAIRING
    PairingState.Paired -> PAIRED_NOW
    is PairingState.Failed -> state.message
}

/** The Pairing section: [shown] means instructions, button and manual entry are visible, [problem] is a red line. */
data class PairingSection(
    val shown: Boolean,
    val connect: String,
    val primary: Boolean,
    val line: String?,
    val problem: Boolean,
)

/** Paired and the service idle: nothing to show, nor the service's past outcomes; [reconnect] means a launch found no pairing. */
fun pairingSectionState(paired: Boolean, serviceState: PairingState, reconnect: Boolean = false): PairingSection {
    val quiet = paired && !reconnect && !serviceState.inProgress
    return PairingSection(
        shown = !quiet,
        connect = if (paired) RECONNECT else CONNECT,
        primary = !paired || reconnect,
        line = if (quiet) null else pairingLine(serviceState),
        problem = !quiet && (serviceState is PairingState.Failed || (serviceState as? PairingState.Searching)?.windowClosed == true),
    )
}

/** Helper buttons: [launch] is the launch caption, [reconnect] is Reconnect, [forget] is the "⋮" menu item. */
data class HelperActions(val launch: String?, val reconnect: Boolean, val forget: Boolean)

/** Only what is needed: a running current helper needs no buttons; [lastLaunch] is the last launch outcome. */
fun helperActions(state: HelperState, paired: Boolean, lastLaunch: LaunchResult?): HelperActions {
    val reconnect = paired && lastLaunch == LaunchResult.NotPaired
    val idle = (state as? HelperState.Running)?.takeIf { !it.status.recording }
    val launch = when {
        !paired || reconnect -> null
        state is HelperState.NotRunning -> LAUNCH
        idle != null && (idle.outdated || idle.status.error.isNotBlank()) -> RELAUNCH
        else -> null
    }
    return HelperActions(launch, reconnect, forget = paired)
}

/** The service has just paired again: the earlier "not paired" launch refusal no longer holds. */
fun launchAfterPairing(last: LaunchResult?, service: PairingState): LaunchResult? =
    last.takeUnless { it == LaunchResult.NotPaired && service == PairingState.Paired }

/** Port for the manual entry field: only the one the service sees now; null while pairing, leave the field alone. */
fun manualPort(state: PairingState): String? = when (state) {
    is PairingState.WaitingCode -> state.port.toString()
    PairingState.Pairing -> null
    else -> ""
}

/** Port from the field: a number or "address:port", as the code window shows it. */
fun parsePort(text: String): Int? = text.trim().substringAfterLast(':').toIntOrNull()?.takeIf { it in 1..65535 }

/** Settings → Helper: what it is for, status with launch, pairing only while it is needed. */
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun HelperGroup(vm: SettingsViewModel, modifier: Modifier = Modifier) {
    val helper by vm.helperState.collectAsStateWithLifecycle()
    val pairing by PairingService.state.collectAsStateWithLifecycle()
    var open by rememberSaveable { mutableStateOf(false) }
    val colors = MaterialTheme.colorScheme
    val actions = helperActions(helper, vm.paired, vm.lastLaunch)
    val section = pairingSectionState(vm.paired, pairing, actions.reconnect)
    FollowPairing(vm)
    // a launch found no pairing: the section expands by itself
    LaunchedEffect(actions.reconnect) { if (actions.reconnect) open = true }
    SettingsGroup(TITLE, modifier) {
        Note(HELPER_PURPOSE, colors.onSurfaceVariant, Modifier.padding(horizontal = 16.dp, vertical = 8.dp))
        SettingsRow(Icons.Rounded.Adb, colors.primary, STATE, helperLine(helper)) {
            when {
                vm.launching -> LoadingIndicator(Modifier.size(LOADING_SIZE))
                actions.launch != null -> FilledTonalButton(onClick = vm::launchHelper) { Text(actions.launch) }
            }
        }
        vm.helperLine?.let { LineNote(it, Modifier.padding(horizontal = 16.dp, vertical = 4.dp)) }
        val subtitle = if (vm.paired) PAIRED else NOT_PAIRED
        if (section.shown) {
            ExpandRow(Icons.Rounded.Link, colors.secondary, PAIRING_ROW, subtitle, open, { open = !open }) {
                PairingControls(vm, section)
            }
        } else {
            SettingsRow(Icons.Rounded.Link, colors.secondary, PAIRING_ROW, subtitle) { if (actions.forget) PairingMenu(vm) }
        }
    }
}

/** The helper in the first-launch wizard: everything in one block inside the step. */
@Composable
fun HelperControls(vm: SettingsViewModel) {
    val helper by vm.helperState.collectAsStateWithLifecycle()
    val pairing by PairingService.state.collectAsStateWithLifecycle()
    val colors = MaterialTheme.colorScheme
    val actions = helperActions(helper, vm.paired, vm.lastLaunch)
    val section = pairingSectionState(vm.paired, pairing, actions.reconnect)
    FollowPairing(vm)
    Note(if (vm.paired) PAIRED else NOT_PAIRED, colors.onSurface)
    Note(helperLine(helper), colors.onSurfaceVariant)
    if (section.shown) PairingControls(vm, section)
    actions.launch?.let { FilledTonalButton(onClick = vm::launchHelper, enabled = !vm.launching) { Text(it) } }
    vm.helperLine?.let { LineNote(it) }
}

// "paired" is a key on disk: reread after pairing and on return to the screen
@Composable
private fun FollowPairing(vm: SettingsViewModel) {
    val pairing by PairingService.state.collectAsStateWithLifecycle()
    val refresh = {
        // before clearOutcome: after it Paired is no longer visible and the launch refusal would remain
        vm.refreshPaired()
        if (vm.paired) PairingService.clearOutcome()
    }
    LaunchedEffect(pairing) { refresh() }
    LifecycleResumeEffect(vm) {
        refresh()
        onPauseOrDispose {}
    }
}

@Composable
private fun PairingControls(vm: SettingsViewModel, section: PairingSection) {
    val context = LocalContext.current
    val pairing by PairingService.state.collectAsStateWithLifecycle()
    val access = rememberAccess()
    val actions = rememberAccessActions()
    var manual by rememberSaveable { mutableStateOf(false) }
    val colors = MaterialTheme.colorScheme
    if (!access.notifications) {
        Note(NO_NOTIFICATIONS, colors.error)
        Button(onClick = actions.notifications) { Text(ALLOW_NOTIFICATIONS) }
    }
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        (STEPS.mapIndexed { i, step -> "${i + 1}. $step" } + NOTES).forEach { Note(it, colors.onSurfaceVariant) }
    }
    val connect = {
        vm.pair()
        open(context, developerSettings())
    }
    // the code is entered in the notification; without notifications only manual entry is left
    when {
        section.primary && access.notifications -> Button(onClick = connect) { Text(section.connect) }
        section.primary -> OutlinedButton(onClick = connect) { Text(section.connect) }
        else -> FilledTonalButton(onClick = connect) { Text(section.connect) }
    }
    section.line?.let { Note(it, if (section.problem) colors.error else colors.onSurfaceVariant) }
    when {
        access.notifications -> {
            TextButton(onClick = { manual = !manual }, contentPadding = ButtonDefaults.TextButtonWithIconContentPadding) {
                Text(MANUAL)
                Icon(
                    if (manual) Icons.Rounded.ExpandLess else Icons.Rounded.ExpandMore,
                    contentDescription = null,
                    modifier = Modifier.padding(start = 8.dp),
                )
            }
            AnimatedVisibility(manual) {
                Column(verticalArrangement = Arrangement.spacedBy(12.dp)) { ManualPairing(vm, pairing) }
            }
        }
        else -> ManualPairing(vm, pairing)
    }
}

@Composable
private fun PairingMenu(vm: SettingsViewModel) {
    var menu by remember { mutableStateOf(false) }
    var confirm by rememberSaveable { mutableStateOf(false) }
    Box {
        IconButton(onClick = { menu = true }) { Icon(Icons.Rounded.MoreVert, contentDescription = MORE) }
        DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
            DropdownMenuItem(
                text = { Text(FORGET) },
                onClick = {
                    menu = false
                    confirm = true
                },
            )
        }
    }
    if (confirm) {
        AlertDialog(
            onDismissRequest = { confirm = false },
            title = { Text(FORGET_TITLE) },
            confirmButton = {
                TextButton(onClick = {
                    confirm = false
                    vm.forgetPairing()
                }) { Text(FORGET_CONFIRM) }
            },
            dismissButton = { TextButton(onClick = { confirm = false }) { Text(CANCEL) } },
        )
    }
}

@Composable
private fun ManualPairing(vm: SettingsViewModel, pairing: PairingState) {
    val port = rememberTextFieldState()
    val code = rememberTextFieldState()
    LaunchedEffect(pairing) { manualPort(pairing)?.let(port::setTextAndPlaceCursorAtEnd) }
    Note(SPLIT_SCREEN, MaterialTheme.colorScheme.onSurfaceVariant)
    OutlinedTextField(
        state = port,
        modifier = Modifier.fillMaxWidth(),
        label = { Text(PORT) },
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
        lineLimits = TextFieldLineLimits.SingleLine,
    )
    OutlinedTextField(
        state = code,
        modifier = Modifier.fillMaxWidth(),
        label = { Text(CODE) },
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
        lineLimits = TextFieldLineLimits.SingleLine,
    )
    Button(onClick = { vm.pairManually(port.text.toString(), code.text.toString()) }) { Text(CONNECT_MANUAL) }
}

@Composable
private fun Note(text: String, color: Color, modifier: Modifier = Modifier) {
    Text(text, modifier, style = MaterialTheme.typography.bodyMedium, color = color)
}

private val LOADING_SIZE = 40.dp
