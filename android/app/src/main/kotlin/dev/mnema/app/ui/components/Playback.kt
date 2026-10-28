package dev.mnema.app.ui.components

import java.math.BigDecimal
import kotlin.math.abs

internal val SPEEDS = listOf(1f, 1.25f, 1.5f, 1.75f, 2f)
internal const val SKIP_MS = 15_000L
internal const val SYNC_MS = 1_000L
internal const val MAX_DRIFT_MS = 150L

/** The menu speed closest to the saved one; an unknown value gives 1×. */
fun speedChoice(stored: Float): Float = if (stored.isNaN()) SPEEDS.first() else SPEEDS.minBy { abs(it - stored) }

fun speedLabel(speed: Float): String = BigDecimal(speed.toString()).stripTrailingZeros().toPlainString().replace('.', ',') + "×"

/** While the player reads the audio header, the length is already known from the recording card. */
fun playbackLength(playerMs: Long, recordingMs: Long): Long = playerMs.takeIf { it > 0 } ?: recordingMs.coerceAtLeast(0)

/** Position after seeking by [deltaMs]: not before the start and not past the end. */
fun skipTarget(positionMs: Long, deltaMs: Long, durationMs: Long): Long = (positionMs + deltaMs).coerceIn(0L, maxOf(durationMs, 0L))

/** Where to seek the second track so it plays along with the first; null when the drift is small or the track has ended. */
fun resyncTarget(leaderMs: Long, followerMs: Long, followerLengthMs: Long): Long? {
    if (followerLengthMs <= 0 || leaderMs >= followerLengthMs) return null
    return leaderMs.takeIf { abs(leaderMs - followerMs) > MAX_DRIFT_MS }
}
