package dev.mnema.app.ui.onboarding

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ArrowBack
import androidx.compose.material.icons.rounded.Adb
import androidx.compose.material.icons.rounded.AutoAwesome
import androidx.compose.material.icons.rounded.BatteryChargingFull
import androidx.compose.material.icons.rounded.Check
import androidx.compose.material.icons.rounded.Dns
import androidx.compose.material.icons.rounded.Folder
import androidx.compose.material.icons.rounded.Notifications
import androidx.compose.material3.Button
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.toShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.mnema.app.AppGraph
import dev.mnema.app.i18n.tr
import dev.mnema.app.launch.PairingService
import dev.mnema.app.ui.components.HeroHeader
import dev.mnema.app.ui.components.SettingsGroup
import dev.mnema.app.ui.components.SettingsRow
import dev.mnema.app.ui.settings.ExpandRow
import dev.mnema.app.ui.settings.HELPER_PURPOSE
import dev.mnema.app.ui.settings.HelperControls
import dev.mnema.app.ui.settings.ServerControls
import dev.mnema.app.ui.settings.SettingsViewModel

private val TITLE: String get() = tr("Getting started")
private val WHO_SEES: String get() = tr("Recordings are kept in Documents/Mnema, where apps with all files access can see them")
private val REQUIRED: String get() = tr("Required")
private val LATER: String get() = tr("Optional")
private val DONE: String get() = tr("Done")
private val BACK: String get() = tr("Back")
private val ALLOW: String get() = tr("Allow")
private val TURN_ON: String get() = tr("Turn on")
private val FILES_TEXT: String get() = tr("Without it recordings cannot be read or processed")
private val NOTIFICATIONS_TEXT: String get() = tr("Calls, recording and finished transcripts")
private val BATTERY_TEXT: String get() = tr("Background processing is not interrupted")

private const val STEP_FILES = "files"
private const val STEP_NOTIFICATIONS = "notifications"
private const val STEP_BATTERY = "battery"
private const val STEP_HELPER = "helper"
private const val STEP_SERVER = "server"

data class OnboardingStep(val id: String, val title: String, val done: Boolean, val required: Boolean)

fun onboardingSteps(
    files: Boolean,
    notifications: Boolean,
    battery: Boolean,
    paired: Boolean,
    server: Boolean,
): List<OnboardingStep> = listOf(
    OnboardingStep(STEP_FILES, tr("All files access"), files, required = true),
    OnboardingStep(STEP_NOTIFICATIONS, tr("Notifications"), notifications, required = false),
    OnboardingStep(STEP_BATTERY, tr("Unrestricted battery"), battery, required = false),
    OnboardingStep(STEP_HELPER, tr("Helper"), paired, required = false),
    OnboardingStep(STEP_SERVER, tr("Server"), server, required = false),
)

fun canFinish(steps: List<OnboardingStep>): Boolean = steps.all { it.done || !it.required }

/** Step note: done, required or optional; recording works even without a server and a model. */
fun stepNote(step: OnboardingStep): String = when {
    step.done -> DONE
    step.required -> REQUIRED
    else -> LATER
}

private val PERMISSION_STEPS = setOf(STEP_FILES, STEP_NOTIFICATIONS, STEP_BATTERY)

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun OnboardingScreen(graph: AppGraph, onDone: () -> Unit, onBack: (() -> Unit)?, modifier: Modifier = Modifier) {
    val vm: SettingsViewModel = viewModel { SettingsViewModel(graph) }
    val settings by vm.settings.collectAsStateWithLifecycle()
    val pairing by PairingService.state.collectAsStateWithLifecycle()
    val access = rememberAccess()
    val actions = rememberAccessActions()
    LaunchedEffect(pairing) { vm.refreshPaired() }
    LifecycleResumeEffect(vm) {
        vm.refreshPaired()
        onPauseOrDispose {}
    }
    val steps = onboardingSteps(
        files = access.files,
        notifications = access.notifications,
        battery = access.battery,
        paired = vm.paired,
        server = settings?.let { graph.runner(it) } != null,
    )
    val next = steps.firstOrNull { !it.done }?.id
    var open by rememberSaveable { mutableStateOf(next) }
    // back from Android settings with the access granted: the next step is expanded
    val openDone = steps.any { it.id == open && it.done && it.id in PERMISSION_STEPS }
    LaunchedEffect(openDone) { if (openDone) open = next }

    Scaffold(
        modifier = modifier.fillMaxSize(),
        bottomBar = {
            Surface(color = MaterialTheme.colorScheme.surfaceContainer) {
                Button(
                    onClick = { vm.finishOnboarding(onDone) },
                    enabled = canFinish(steps),
                    modifier = Modifier
                        .fillMaxWidth()
                        .navigationBarsPadding()
                        .padding(16.dp),
                ) { Text(DONE) }
            }
        },
        contentWindowInsets = WindowInsets(0),
    ) { padding ->
        Column(
            Modifier
                .padding(padding)
                .fillMaxSize()
                .imePadding()
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            HeroHeader(decoration = MaterialShapes.Cookie9Sided.toShape()) {
                if (onBack != null) {
                    IconButton(onClick = onBack, modifier = Modifier.offset(x = (-12).dp)) {
                        Icon(Icons.AutoMirrored.Rounded.ArrowBack, contentDescription = BACK)
                    }
                }
                Text(
                    TITLE,
                    Modifier.semantics { heading() },
                    style = MaterialTheme.typography.displaySmallEmphasized,
                )
                Text(
                    WHO_SEES,
                    style = MaterialTheme.typography.bodyMedium,
                    color = LocalContentColor.current.copy(alpha = NOTE_ALPHA),
                )
            }
            Column(
                Modifier.padding(start = 16.dp, end = 16.dp, bottom = 16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                val colors = MaterialTheme.colorScheme
                steps.forEach { step ->
                    val expanded = open == step.id
                    SettingsGroup(title = null) {
                        ExpandRow(
                            icon = if (step.done) Icons.Rounded.Check else stepIcon(step.id),
                            tint = stepTint(step, colors),
                            title = step.title,
                            subtitle = stepNote(step),
                            expanded = expanded,
                            onToggle = { open = if (expanded) null else step.id },
                        ) {
                            when (step.id) {
                                STEP_FILES -> Action(FILES_TEXT, ALLOW, actions.files)
                                STEP_NOTIFICATIONS -> Action(NOTIFICATIONS_TEXT, TURN_ON, actions.notifications)
                                STEP_BATTERY -> Action(BATTERY_TEXT, ALLOW, actions.battery)
                                STEP_HELPER -> {
                                    StepText(HELPER_PURPOSE)
                                    HelperControls(vm)
                                }
                                STEP_SERVER -> ServerControls(vm)
                            }
                        }
                    }
                }
            }
        }
    }
}

private fun stepIcon(id: String): ImageVector = when (id) {
    STEP_FILES -> Icons.Rounded.Folder
    STEP_NOTIFICATIONS -> Icons.Rounded.Notifications
    STEP_BATTERY -> Icons.Rounded.BatteryChargingFull
    STEP_HELPER -> Icons.Rounded.Adb
    STEP_SERVER -> Icons.Rounded.Dns
    else -> Icons.Rounded.AutoAwesome
}

// a done step is primary with a check, a missing required one is error, the rest use their own color
private fun stepTint(step: OnboardingStep, colors: ColorScheme): Color = when {
    step.done -> colors.primary
    step.required -> colors.error
    else -> colors.secondary
}

@Composable
private fun Action(text: String, button: String, onClick: () -> Unit) {
    StepText(text)
    Button(onClick = onClick) { Text(button) }
}

@Composable
private fun StepText(text: String) {
    Text(text, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
}

private const val NOTE_ALPHA = 0.8f
