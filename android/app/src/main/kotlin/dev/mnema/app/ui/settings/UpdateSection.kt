package dev.mnema.app.ui.settings

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.SystemUpdate
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.LinearWavyProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.mnema.app.AppGraph
import dev.mnema.app.helper.HelperState
import dev.mnema.app.i18n.tr
import dev.mnema.app.ui.components.SettingsGroup
import dev.mnema.app.ui.components.SettingsRow
import dev.mnema.app.ui.onboarding.open
import dev.mnema.app.ui.record.helperClient
import dev.mnema.app.ui.recordings.dayLabel
import dev.mnema.app.update.UpdateStage
import dev.mnema.app.update.installPermissionIntent
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import kotlin.math.max
import kotlin.math.roundToInt
import kotlin.math.roundToLong
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.map

private val TITLE: String get() = tr("Updates")
private val CHECKING: String get() = tr("Checking…")
private val NEEDS_PERMISSION: String get() = tr("Allow Mnema to install apps")
private val INSTALLING: String get() = tr("Installing…")
private val CONFIRM: String get() = tr("Confirm the installation")
private val AFTER_RECORDING: String get() = tr("Available after recording")

private const val BYTES_IN_MB = 1024.0 * 1024
private const val TRACK_ALPHA = 0.24f

// the progress bar sits under the row caption, not under the tile
private val TEXT_INSET = 72.dp

enum class UpdateAction {
    CHECK,
    UPDATE,
    INSTALL,
    ALLOW,
    ;

    val title: String
        get() = when (this) {
            CHECK -> tr("Check")
            UPDATE -> tr("Update")
            INSTALL -> tr("Install")
            ALLOW -> tr("Allow")
        }
}

// a new APK would end the helper's recording: it is installed after it
private val WAITS_FOR_RECORDING = setOf(UpdateAction.UPDATE, UpdateAction.INSTALL)

/** The Updates row: stage caption, button and download progress; busy is progress without percent. */
data class UpdateRow(
    val title: String,
    val subtitle: String?,
    val action: UpdateAction?,
    val progress: Float? = null,
    val busy: Boolean = false,
    val enabled: Boolean = true,
)

/** [recording]: the helper is recording, so update and install are only possible after it. */
fun rowState(version: String, stage: UpdateStage, checkedAt: Long, today: LocalDate, zone: ZoneId, recording: Boolean): UpdateRow {
    val row = stageRow(tr("Version {version}", "version" to version), stage, checkedAt, today, zone)
    return if (recording && row.action in WAITS_FOR_RECORDING) row.copy(subtitle = AFTER_RECORDING, enabled = false) else row
}

private fun stageRow(title: String, stage: UpdateStage, checkedAt: Long, today: LocalDate, zone: ZoneId): UpdateRow {
    return when (stage) {
        UpdateStage.Idle -> UpdateRow(title, checked(checkedAt, today, zone), UpdateAction.CHECK)
        UpdateStage.Checking -> UpdateRow(title, CHECKING, null, busy = true)
        UpdateStage.UpToDate ->
            UpdateRow(title, checked(checkedAt, today, zone) ?: checked(today, today), UpdateAction.CHECK)
        is UpdateStage.Unsuitable -> UpdateRow(title, tr("{version} is not compatible with this phone", "version" to stage.manifest.version), UpdateAction.CHECK)
        is UpdateStage.Available -> UpdateRow(
            title,
            tr("{version} available · {size} MB", "version" to stage.manifest.version, "size" to megabytes(stage.manifest.size)),
            UpdateAction.UPDATE,
        )
        is UpdateStage.Downloading -> UpdateRow(
            title,
            tr("Downloading… {percent}%", "percent" to (stage.fraction.coerceIn(0f, 1f) * 100).roundToInt()),
            null,
            progress = stage.fraction,
        )
        is UpdateStage.NeedsPermission -> UpdateRow(title, NEEDS_PERMISSION, UpdateAction.ALLOW)
        is UpdateStage.Ready -> UpdateRow(title, tr("{version} downloaded", "version" to stage.manifest.version), UpdateAction.INSTALL)
        is UpdateStage.Installing -> UpdateRow(title, INSTALLING, null, busy = true)
        is UpdateStage.Confirm -> UpdateRow(title, CONFIRM, UpdateAction.INSTALL)
        is UpdateStage.Failed -> UpdateRow(title, stage.text, retry(stage))
    }
}

private fun retry(failed: UpdateStage.Failed): UpdateAction = when {
    failed.apk != null -> UpdateAction.INSTALL
    failed.manifest != null -> UpdateAction.UPDATE
    else -> UpdateAction.CHECK
}

private fun checked(at: Long, today: LocalDate, zone: ZoneId): String? =
    if (at <= 0) null else checked(Instant.ofEpochMilli(at).atZone(zone).toLocalDate(), today)

// "today" and "yesterday" go lower case after the verb; a date stays as written ("Oct 21")
private fun checked(day: LocalDate, today: LocalDate): String {
    val label = dayLabel(day, today)
    val relative = day == today || day == today.minusDays(1)
    return tr("Checked {day}", "day" to if (relative) label.replaceFirstChar { it.lowercase() } else label)
}

private fun megabytes(size: Long): Long = max(1, (size / BYTES_IN_MB).roundToLong())

@Composable
fun UpdateGroup(graph: AppGraph, modifier: Modifier = Modifier) {
    val updates = graph.updates
    val context = LocalContext.current
    val stage by updates.stage.collectAsStateWithLifecycle()
    val checkedAt by remember(graph) { graph.settings.settings.map { it.updateCheckedAt } }.collectAsStateWithLifecycle(0L)
    val helper by remember(graph) {
        helperClient(graph).getOrNull()?.state ?: MutableStateFlow(HelperState.NotRunning)
    }.collectAsStateWithLifecycle()
    val recording = (helper as? HelperState.Running)?.status?.recording == true
    LifecycleResumeEffect(updates) {
        updates.permissionChanged()
        onPauseOrDispose {}
    }
    val notice by graph.serverNotice.collectAsStateWithLifecycle(null)
    val zone = ZoneId.systemDefault()
    val row = rowState(updates.versionName, stage, checkedAt, LocalDate.now(zone), zone, recording)
    SettingsGroup(TITLE, modifier) {
        SettingsRow(Icons.Rounded.SystemUpdate, MaterialTheme.colorScheme.primary, row.title, row.subtitle) {
            row.action?.let { action ->
                ActionButton(action, row.enabled) {
                    when (action) {
                        UpdateAction.CHECK -> updates.check()
                        UpdateAction.UPDATE -> updates.update()
                        UpdateAction.INSTALL -> updates.install()
                        UpdateAction.ALLOW -> open(context, installPermissionIntent(context))
                    }
                }
            }
        }
        if (row.progress != null || row.busy) {
            UpdateProgress(row.progress, Modifier.padding(start = TEXT_INSET, end = 16.dp, bottom = 12.dp))
        }
        // the chosen server runs another Mnema version: which side to update, next to the app's update
        notice?.let {
            Text(it, Modifier.padding(start = TEXT_INSET, end = 16.dp, bottom = 12.dp),
                color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyMedium)
        }
    }
}

@Composable
private fun ActionButton(action: UpdateAction, enabled: Boolean, onClick: () -> Unit) {
    if (action == UpdateAction.CHECK) {
        TextButton(onClick = onClick, enabled = enabled) { Text(action.title) }
    } else {
        FilledTonalButton(onClick = onClick, enabled = enabled) { Text(action.title) }
    }
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun UpdateProgress(progress: Float?, modifier: Modifier) {
    val color = MaterialTheme.colorScheme.primary
    val track = color.copy(alpha = TRACK_ALPHA)
    if (progress != null) {
        val shown by animateFloatAsState(progress.coerceIn(0f, 1f), MaterialTheme.motionScheme.slowEffectsSpec())
        LinearWavyProgressIndicator(progress = { shown }, modifier = modifier.fillMaxWidth(), color = color, trackColor = track)
    } else {
        LinearWavyProgressIndicator(modifier.fillMaxWidth(), color = color, trackColor = track)
    }
}
