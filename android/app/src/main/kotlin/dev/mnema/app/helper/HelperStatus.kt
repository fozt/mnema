package dev.mnema.app.helper

import kotlinx.serialization.Required
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Helper heartbeat from status.json, protocol v:1. */
@Serializable
data class HelperStatus(
    val v: Int = 0,
    val state: String = "",
    val nonce: String = "",
    @SerialName("apk_updated") val apkUpdated: Long = 0,
    @SerialName("heartbeat_ms") val heartbeatMs: Long = 0,
    @SerialName("heartbeat_uptime_ms") val heartbeatUptimeMs: Long = 0,
    val pong: String = "",
    val folder: String = "",
    @SerialName("last_folder") val lastFolder: String = "",
    // in the interface language once read by HelperClient; status.json has it in English
    val error: String = "",
    @SerialName("started_ms") val startedMs: Long = 0,
    @SerialName("call_active") val callActive: Boolean = false,
    // none, ringing — an incoming call, cellular or messenger — a conversation
    @SerialName("call_mode") val callMode: String = "",
    @Required @SerialName("mic_on") val micOn: Boolean = true,
    @Required @SerialName("call_on") val callOn: Boolean = true,
    @Required val paused: Boolean = false,
    @SerialName("paused_ms") val pausedMs: Long = 0,
    @SerialName("call_rms") val callRms: Double? = null,
    @SerialName("mic_rms") val micRms: Double? = null,
    @SerialName("mic_silenced") val micSilenced: Boolean? = null,
    // non-zero samples in the last 3 s; null — the recording has not started
    @SerialName("call_sound") val callSound: Boolean? = null,
    @SerialName("mic_sound") val micSound: Boolean? = null,
    // for how many ms in a row the other side has been zeros, during a call counted from its start
    @SerialName("call_silent_ms") val callSilentMs: Long? = null,
) {
    val recording: Boolean get() = state == "recording"

    /** An incoming call: the phone rings, no conversation yet. */
    val ringing: Boolean get() = callMode == "ringing"

    /** How much is recorded by [nowMs] without pauses; while paused the time stands at the heartbeat, not started — 0. */
    fun recordedMs(nowMs: Long): Long =
        if (startedMs <= 0) 0 else ((if (paused) heartbeatMs else nowMs) - startedMs - pausedMs).coerceAtLeast(0)
}

sealed interface HelperState {
    data object Unknown : HelperState
    data object NotRunning : HelperState
    data class Running(val status: HelperStatus, val outdated: Boolean) : HelperState
}

enum class HelperCommand(val wire: String) {
    START("start"),
    STOP("stop"),
    PING("ping"),
    MIC_ON("mic-on"),
    MIC_OFF("mic-off"),
    CALL_ON("call-on"),
    CALL_OFF("call-off"),
    PAUSE("pause"),
    RESUME("resume"),
}

/** Which tracks are recorded with sound: a muted one is recorded as silence, both files stay. */
data class SourceChoice(val mic: Boolean = true, val call: Boolean = true)
