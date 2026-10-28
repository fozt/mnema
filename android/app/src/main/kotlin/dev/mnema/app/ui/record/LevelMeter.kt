package dev.mnema.app.ui.record

import kotlin.math.log10

private const val FULL_SCALE = 32768.0
private const val FLOOR_DB = -60.0
private const val CEIL_DB = -10.0

/** RMS of PCM16 samples → 0..1: −60 dBFS and quieter is empty, −10 dBFS and louder is full. */
fun level(rms: Double?): Float {
    if (rms == null || rms.isNaN() || rms <= 0.0) return 0f
    val db = 20 * log10(rms / FULL_SCALE)
    return ((db - FLOOR_DB) / (CEIL_DB - FLOOR_DB)).coerceIn(0.0, 1.0).toFloat()
}
