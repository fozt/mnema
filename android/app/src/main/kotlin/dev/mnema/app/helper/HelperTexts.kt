package dev.mnema.app.helper

import dev.mnema.app.i18n.tr

/**
 * A helper text from status.json or the recording report in the interface language. The helper runs in its own
 * process and does not know the app language, so it writes English (Heartbeat, HelperMain, Capture, Failures) and
 * the app translates it here: every stage the helper writes needs its branch below. An unknown text, such as one
 * written by an older helper, is shown as is.
 */
fun helperText(text: String): String {
    if (text.startsWith(Heartbeat.FAILED_TO_START)) {
        return tr("Couldn't start recording: {reason}", "reason" to helperReason(text.removePrefix(Heartbeat.FAILED_TO_START)))
    }
    if (text.startsWith(Heartbeat.STOPPED_BY_ERROR)) {
        return tr("Recording stopped because of an error: {reason}", "reason" to helperReason(text.removePrefix(Heartbeat.STOPPED_BY_ERROR)))
    }
    return when (text) {
        HelperMain.UNKNOWN_COMMAND -> tr("The helper didn't understand the command. Restart it")
        HelperMain.APP_UPDATED -> tr("The helper stopped: the app was updated")
        else -> helperReason(text)
    }
}

/** A failure stage (Capture, Failures, HelperMain), lower case: it goes after a colon. */
private fun helperReason(text: String): String = when (text) {
    Capture.NO_SYSTEM_AUDIO -> tr("no access to system audio")
    Capture.MIC_UNAVAILABLE -> tr("microphone unavailable")
    Capture.NO_FOLDER_ACCESS -> tr("no access to the Documents/Mnema folder")
    Capture.FILES_NOT_CREATED -> tr("recording files can't be created")
    Capture.SYSTEM_AUDIO_NOT_RECORDING -> tr("system audio isn't recording")
    Capture.MIC_NOT_RECORDING -> tr("microphone isn't recording")
    Capture.FILES_NOT_WRITTEN -> tr("recording files can't be written")
    Capture.SYSTEM_AUDIO_LOST -> tr("system audio was lost")
    Capture.MIC_LOST -> tr("microphone was lost")
    Failures.FILES_NOT_CLOSED -> tr("recording files were not saved completely")
    HelperMain.INTERNAL_FAILURE -> tr("helper failure, restart it")
    else -> text
}

