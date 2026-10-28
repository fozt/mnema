package dev.mnema.app.ui.recordings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.LocalTextStyle
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RangeSlider
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.ToggleButton
import androidx.compose.material3.RangeSliderState
import androidx.compose.material3.SheetValue
import androidx.compose.material3.rememberBottomSheetState
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.listSaver
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import dev.mnema.app.i18n.tr
import dev.mnema.app.model.AUTO_ASR
import dev.mnema.app.server.AsrOption
import dev.mnema.app.model.Lang
import dev.mnema.app.processing.ReprocessOptions
import dev.mnema.app.ui.components.durationLabel
import dev.mnema.app.recordings.RecordingDetail
import java.time.LocalDate
import java.time.ZoneId
import kotlinx.coroutines.launch
import dev.mnema.app.transcript.RecordingFolder

private val LANG: String get() = tr("Language")
private val ASR: String get() = tr("Recognition")
private val SOURCES: String get() = tr("Sources")
private val PART: String get() = tr("Part of recording")
private val START: String get() = tr("Start")
private val END: String get() = tr("End")
private val NAME: String get() = tr("Title")
private val NAME_PLACEHOLDER: String get() = tr("Recording title")
private val CANCEL: String get() = tr("Cancel")
private val PROCESS: String get() = tr("Process")

private val PartFormSaver = listSaver<PartForm, Any>(
    save = { listOf(it.length, it.start, it.end, it.startText, it.endText) },
    restore = { PartForm(it[0] as Double, it[1] as Double, it[2] as Double, it[3] as String, it[4] as String) },
)

/**
 * The dialog for any manual processing, like desktop's Process recording…: the choice applies only to this processing.
 * [onConfirm] gets the choice and whether it stayed as the dialog opened with.
 */
@OptIn(ExperimentalMaterial3Api::class, ExperimentalMaterial3ExpressiveApi::class, ExperimentalLayoutApi::class)
@Composable
fun ReprocessSheet(
    detail: RecordingDetail,
    start: SheetStart,
    heading: String,
    onDismiss: () -> Unit,
    onConfirm: (ReprocessOptions, Boolean) -> Unit,
    loadAsrChoices: suspend (String) -> List<AsrOption>,
    loadSoundTracks: suspend () -> Set<String>,
) {
    val item = detail.item
    val key = item.dir.path
    // a custom title is one that differs from the folder name, as in the desktop dialog
    val current = rememberSaveable(key) { shownTitle(detail) }
    var lang by rememberSaveable(key) { mutableStateOf(start.lang) }
    var asr by rememberSaveable(key) { mutableStateOf(start.asr) }
    var asrChoices by remember(key) { mutableStateOf(fallbackAsrChoices(asr)) }
    // tracks with audio are the default choice, the server decides on silence
    var audible by rememberSaveable(key) { mutableStateOf<List<String>?>(null) }
    var sources by rememberSaveable(key) { mutableStateOf(start.tracks?.toList()) }
    LaunchedEffect(key) {
        if (start.tracks != null) return@LaunchedEffect
        val found = audible ?: loadSoundTracks().toList().also { audible = it }
        if (sources == null) sources = found
    }
    LaunchedEffect(key) {
        asrChoices = loadAsrChoices(asr).ifEmpty { fallbackAsrChoices(asr) }
        if (asrChoices.none { it.id == asr }) asr = asrChoices.firstOrNull { it.id == AUTO_ASR }?.id ?: asrChoices.first().id
    }
    var title by rememberSaveable(key) { mutableStateOf(start.title) }
    var part by rememberSaveable(key, stateSaver = PartFormSaver) { mutableStateOf(PartForm(item.durationS)) }
    val choice = part.choice()
    val options = sources?.let { reprocessOptions(lang, asr, title, current, choice, it.toSet()) }
    // "unchanged" is relative to what the dialog opened with
    val initial = (start.tracks ?: audible?.toSet())?.let { reprocessOptions(start.lang, start.asr, start.title, current, PartChoice.Whole, it) }

    // no half-expanded state: the sheet opens fully
    val sheet = rememberBottomSheetState(SheetValue.Hidden, setOf(SheetValue.Hidden, SheetValue.Expanded))
    val scope = rememberCoroutineScope()
    val close = { then: () -> Unit ->
        scope.launch { sheet.hide() }.invokeOnCompletion { if (!sheet.isVisible) then() }
    }

    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = sheet) {
        Column(
            Modifier
                .fillMaxWidth()
                .verticalScroll(rememberScrollState())
                .padding(start = 24.dp, end = 24.dp, bottom = 24.dp),
            verticalArrangement = Arrangement.spacedBy(24.dp),
        ) {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(heading, modifier = Modifier.semantics { heading() }, style = MaterialTheme.typography.headlineSmallEmphasized)
                val zone = remember { ZoneId.systemDefault() }
                Text(
                    "${recordingLabel(item, LocalDate.now(zone), zone)} · ${durationLabel(item.durationS)}",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Section(LANG) { Choices(Lang.entries, lang, Lang::title) { lang = it } }
            Section(SOURCES) {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    RecordingFolder.TRACK_NAMES.forEach { (label, name) ->
                        if (detail.tracks.any { it.nameWithoutExtension == label }) {
                            ToggleButton(checked = label in sources.orEmpty(), enabled = sources != null, onCheckedChange = {
                                sources = selectedTracks(sources.orEmpty().toSet(), label).toList()
                            }) { Text(name) }
                        }
                    }
                }
            }
            Section(ASR) { Choices(asrChoices, asrChoices.firstOrNull { it.id == asr }, AsrOption::title) { asr = it.id } }
            Section(PART) { PartFields(part, choice) { change -> part = change(part) } }
            OutlinedTextField(
                value = title,
                onValueChange = { title = it },
                modifier = Modifier.fillMaxWidth(),
                label = { Text(NAME) },
                placeholder = { Text(NAME_PLACEHOLDER) },
                singleLine = true,
                keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Sentences, imeAction = ImeAction.Done),
            )
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.End)) {
                TextButton(onClick = { close(onDismiss) }, shapes = ButtonDefaults.shapes()) { Text(CANCEL) }
                Button(
                    onClick = { options?.let { chosen -> close { onConfirm(chosen, chosen == initial) } } },
                    enabled = options != null,
                    shapes = ButtonDefaults.shapes(),
                ) { Text(PROCESS) }
            }
        }
    }
}

@Composable
private fun Section(title: String, content: @Composable ColumnScope.() -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(
            title,
            modifier = Modifier.semantics { heading() },
            style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.primary,
        )
        content()
    }
}

// one of the options: the selected one is filled and changes shape
@OptIn(ExperimentalMaterial3ExpressiveApi::class, ExperimentalLayoutApi::class)
@Composable
private fun <T> Choices(options: List<T>, selected: T?, label: (T) -> String, onSelect: (T) -> Unit) {
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        options.forEach { option ->
            ToggleButton(checked = option == selected, onCheckedChange = { onSelect(option) }) { Text(label(option)) }
        }
    }
}

// a two-thumb slider and h:mm:ss fields; under them how much is selected or what is wrong
@Composable
private fun PartFields(form: PartForm, choice: PartChoice, update: ((PartForm) -> PartForm) -> Unit) {
    val colors = MaterialTheme.colorScheme
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        val slider = remember(form.length) { RangeSliderState(form.start.toFloat(), form.end.toFloat(), 0, 0f..form.length.toFloat()) }
        // the slider thumbs follow the fields: a typed time moves them too
        SideEffect { slider.follow(form) }
        RangeSlider(
            state = slider,
            onValueChange = { range -> update { it.slide(range.start.toDouble(), range.endInclusive.toDouble()) } },
        )
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            TimeField(START, form.startText, last = false, Modifier.weight(1f), { text -> update { it.typeStart(text) } }) {
                update { it.commitStart() }
            }
            TimeField(END, form.endText, last = true, Modifier.weight(1f), { text -> update { it.typeEnd(text) } }) {
                update { it.commitEnd() }
            }
        }
        val problem = (choice as? PartChoice.Invalid)?.message
        Text(
            problem ?: form.caption,
            style = MaterialTheme.typography.bodyMedium,
            color = if (problem != null) colors.error else colors.onSurfaceVariant,
        )
    }
}

// a field applies its time when focus leaves it: then the other edge moves too
@Composable
private fun TimeField(label: String, value: String, last: Boolean, modifier: Modifier, onType: (String) -> Unit, onLeave: () -> Unit) {
    val focus = LocalFocusManager.current
    var focused by remember { mutableStateOf(false) }
    OutlinedTextField(
        value = value,
        onValueChange = onType,
        modifier = modifier.onFocusChanged { state ->
            if (focused && !state.isFocused) onLeave()
            focused = state.isFocused
        },
        label = { Text(label) },
        singleLine = true,
        isError = value.isNotBlank() && parseOffset(value) == null,
        textStyle = LocalTextStyle.current.copy(fontFeatureSettings = "tnum"),
        keyboardOptions = KeyboardOptions(
            keyboardType = KeyboardType.Decimal,
            imeAction = if (last) ImeAction.Done else ImeAction.Next,
        ),
        keyboardActions = KeyboardActions(onDone = { focus.clearFocus() }),
    )
}

// the start cannot pass the end: the edge that makes room moves first
private fun RangeSliderState.follow(form: PartForm) {
    val start = form.start.toFloat()
    val end = form.end.toFloat()
    if (start > endValue) {
        endValue = end
        startValue = start
    } else {
        startValue = start
        endValue = end
    }
}
