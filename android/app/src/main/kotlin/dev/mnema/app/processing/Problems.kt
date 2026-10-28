package dev.mnema.app.processing

import dev.mnema.app.i18n.tr

object Problems {
    val NO_NODE: String get() = tr("No server selected. Choose one in Settings under Processing")
    val TOKEN: String get() = tr("A new invitation from the server is needed. Add the server again in Settings")
    val NO_SOUND: String get() = tr("The recording has no sound")
    val NO_TRANSCRIPT: String get() = tr("No transcript. Process the recording")
    val JOB_LOST: String get() = tr("The server didn't keep the processing result. Try again later")
    fun nodeDown(name: String) = tr("{name} is unavailable", "name" to name)
    fun upload(reason: String) = tr("The recording didn't upload to the server: {reason}", "reason" to reason)
    val WRITE: String get() = tr("Couldn't save the result on the phone. Check free space and All files access")
    val PHONE_FAILURE: String get() = tr("Processing failed on the phone. Tap Retry")
}
