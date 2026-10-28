package dev.mnema.app.ui.components

import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.input.KeyboardActionHandler
import androidx.compose.foundation.text.input.TextFieldState
import androidx.compose.foundation.text.input.TextObfuscationMode
import androidx.compose.foundation.text.input.setTextAndPlaceCursorAtEnd
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.ContentPaste
import androidx.compose.material.icons.rounded.Visibility
import androidx.compose.material.icons.rounded.VisibilityOff
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.OutlinedSecureTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboard
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import dev.mnema.app.i18n.tr
import kotlinx.coroutines.launch

private val SHOW: String get() = tr("Show")
private val HIDE: String get() = tr("Hide")
private val PASTE: String get() = tr("Paste")
private val NOTHING_TO_PASTE: String get() = tr("No text in the clipboard")

/** Clipboard text for a secret field, trimmed of spaces and line breaks; empty gives null. */
fun clipText(raw: CharSequence?): String? = raw?.toString()?.trim()?.takeIf { it.isNotEmpty() }

/** A key, token or connection string: hidden by dots, with Paste and Show at the end; it cannot be copied out. */
@Composable
fun SecretField(
    state: TextFieldState,
    label: String,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    placeholder: String? = null,
    supporting: String? = null,
    isError: Boolean = false,
    imeAction: ImeAction = ImeAction.Default,
    onDone: (() -> Unit)? = null,
) {
    var shown by rememberSaveable { mutableStateOf(false) }
    var nothingToPaste by remember { mutableStateOf(false) }
    val clipboard = LocalClipboard.current
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    LaunchedEffect(state) { snapshotFlow { state.text.toString() }.collect { nothingToPaste = false } }
    val paste: () -> Unit = {
        scope.launch {
            val clip = clipboard.getClipEntry()?.clipData?.takeIf { it.itemCount > 0 }
            val text = clipText(clip?.getItemAt(0)?.coerceToText(context))
            if (text == null) nothingToPaste = true else state.setTextAndPlaceCursorAtEnd(text)
        }
    }
    OutlinedSecureTextField(
        state = state,
        modifier = modifier.fillMaxWidth(),
        enabled = enabled,
        label = { Text(label) },
        placeholder = placeholder?.let { { Text(it) } },
        trailingIcon = {
            Row {
                IconButton(onClick = paste, enabled = enabled) { Icon(Icons.Rounded.ContentPaste, contentDescription = PASTE) }
                IconButton(onClick = { shown = !shown }) {
                    Icon(
                        if (shown) Icons.Rounded.VisibilityOff else Icons.Rounded.Visibility,
                        contentDescription = if (shown) HIDE else SHOW,
                    )
                }
            }
        },
        supportingText = (if (nothingToPaste) NOTHING_TO_PASTE else supporting)?.let { { Text(it) } },
        isError = isError && !nothingToPaste,
        textObfuscationMode = if (shown) TextObfuscationMode.Visible else TextObfuscationMode.Hidden,
        keyboardOptions = KeyboardOptions(autoCorrectEnabled = false, keyboardType = KeyboardType.Password, imeAction = imeAction),
        onKeyboardAction = onDone?.let { done -> KeyboardActionHandler { done() } },
    )
}
