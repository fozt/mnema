package dev.mnema.app.ui.recordings

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Check
import androidx.compose.material.icons.rounded.CreateNewFolder
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.unit.dp
import dev.mnema.app.i18n.tr
import dev.mnema.app.recordings.sectionName

internal val MOVE_TO_SECTION: String get() = tr("Move to section")
private val NEW_SECTION: String get() = tr("New section…")
private val NEW_SECTION_TITLE: String get() = tr("New section")
private val NAME: String get() = tr("Name")
private val CREATE: String get() = tr("Create")
private val CANCEL: String get() = tr("Cancel")

/** Section choices: "No section", then the library sections by name; the empty string is the root. */
internal fun sectionChoices(sections: List<String>): List<String> = listOf("") + sections.filter { it.isNotEmpty() }.distinct().sorted()

/**
 * Move to section: sections as a list, the recording's current one is checked and does nothing; "New section…" opens a
 * dialog for the name. [onMove] gets the section and create: create it if missing.
 */
@Composable
internal fun MoveToSectionDialog(current: String, sections: List<String>, onMove: (String, Boolean) -> Unit, onDismiss: () -> Unit) {
    var naming by rememberSaveable { mutableStateOf(false) }
    if (naming) {
        NewSectionDialog(onCreate = { onMove(it, true) }, onDismiss = onDismiss)
        return
    }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(MOVE_TO_SECTION) },
        text = {
            Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState())) {
                sectionChoices(sections).forEach { section ->
                    val here = section == current
                    DropdownMenuItem(
                        text = { Text(section.ifEmpty { NO_SECTION }) },
                        onClick = { if (!here) onMove(section, false) },
                        modifier = Modifier.semantics { selected = here; role = Role.RadioButton },
                        trailingIcon = if (here) {
                            { Icon(Icons.Rounded.Check, contentDescription = null) }
                        } else {
                            null
                        },
                    )
                }
                HorizontalDivider(Modifier.padding(vertical = 4.dp))
                DropdownMenuItem(
                    text = { Text(NEW_SECTION) },
                    onClick = { naming = true },
                    leadingIcon = { Icon(Icons.Rounded.CreateNewFolder, contentDescription = null) },
                )
            }
        },
        confirmButton = {},
        dismissButton = { TextButton(onClick = onDismiss) { Text(CANCEL) } },
    )
}

/** The New section dialog: the name is cleaned by the folder name rule; if nothing is left, Create is disabled. */
@Composable
internal fun NewSectionDialog(onCreate: (String) -> Unit, onDismiss: () -> Unit) {
    var text by rememberSaveable { mutableStateOf("") }
    val focus = remember { FocusRequester() }
    val name = sectionName(text)
    LaunchedEffect(focus) { focus.requestFocus() }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(NEW_SECTION_TITLE) },
        text = {
            OutlinedTextField(
                value = text,
                onValueChange = { text = it },
                label = { Text(NAME) },
                singleLine = true,
                modifier = Modifier.fillMaxWidth().focusRequester(focus),
                keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Sentences, imeAction = ImeAction.Done),
                keyboardActions = KeyboardActions(onDone = { if (name.isNotEmpty()) onCreate(name) }),
            )
        },
        confirmButton = { TextButton(onClick = { onCreate(name) }, enabled = name.isNotEmpty()) { Text(CREATE) } },
        dismissButton = { TextButton(onClick = onDismiss) { Text(CANCEL) } },
    )
}
