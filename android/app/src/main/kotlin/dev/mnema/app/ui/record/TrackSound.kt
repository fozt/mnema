package dev.mnema.app.ui.record

import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.helper.HelperStatus
import dev.mnema.app.i18n.tr

internal val CALL_BLOCKED: String get() = tr("System audio is not recorded")
internal val CALL_BLOCKED_HINT: String get() = tr("This phone does not share call audio")

/** What the running recording's track carries now. */
enum class TrackSound {
    WRITING,
    SILENCE,
    // a call is on and the other side has been zeros for 5 s: even the helper's fallback did not help
    BLOCKED,
    ;

    val label: String
        get() = when (this) {
            WRITING -> tr("Sound")
            SILENCE -> tr("Silence")
            BLOCKED -> tr("Not recording")
        }
}

/** Sound of track [source] from the helper heartbeat; null means nothing to say: no recording, paused or the track is off. */
fun trackSound(status: HelperStatus?, source: Source): TrackSound? {
    if (status == null || !status.recording || status.paused || status.folder.isEmpty()) return null
    val (on, sound) = when (source) {
        Source.CALL -> status.callOn to status.callSound
        Source.MIC -> status.micOn to status.micSound
    }
    if (!on || sound == null) return null
    return when {
        sound -> TrackSound.WRITING
        source == Source.CALL && status.callActive && (status.callSilentMs ?: 0) >= HelperProtocol.SILENT_CALL_MS -> TrackSound.BLOCKED
        else -> TrackSound.SILENCE
    }
}

/** Recording notification line: "System audio: sound · Microphone: silence"; when system audio is blocked, only that. */
fun soundLine(status: HelperStatus?): String? {
    val call = trackSound(status, Source.CALL)
    if (call == TrackSound.BLOCKED) return CALL_BLOCKED
    return listOfNotNull(
        call?.let { "${sourceName(Source.CALL)}: ${it.label.lowercase()}" },
        trackSound(status, Source.MIC)?.let { "${sourceName(Source.MIC)}: ${it.label.lowercase()}" },
    ).joinToString(" · ").ifEmpty { null }
}
