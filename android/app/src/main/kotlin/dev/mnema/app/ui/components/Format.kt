package dev.mnema.app.ui.components

import dev.mnema.app.i18n.tr

fun durationLabel(seconds: Double): String {
    val total = if (seconds.isFinite() && seconds > 0) seconds.toLong() else 0L
    val hours = total / 3600
    val minutes = total / 60 % 60
    val rest = pad(total % 60)
    return if (hours > 0) "$hours:${pad(minutes)}:$rest" else "$minutes:$rest"
}

/** Desktop panel length_label (length-labels-v1): "15 s", "21 min", "1 h 10 min", "2 h". */
fun lengthLabel(seconds: Double): String {
    val total = if (seconds.isFinite() && seconds > 0) seconds.toLong() else 0L
    if (total < 60) return tr("{n} s", "n" to total)
    val hours = total / 3600
    val minutes = total / 60 % 60
    return when {
        hours == 0L -> tr("{n} min", "n" to minutes)
        minutes == 0L -> tr("{h} h", "h" to hours)
        else -> tr("{h} h {m} min", "h" to hours, "m" to minutes)
    }
}

private fun pad(value: Long): String = value.toString().padStart(2, '0')
