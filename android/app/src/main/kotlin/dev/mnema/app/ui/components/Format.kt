package dev.mnema.app.ui.components

fun durationLabel(seconds: Double): String {
    val total = if (seconds.isFinite() && seconds > 0) seconds.toLong() else 0L
    val hours = total / 3600
    val minutes = total / 60 % 60
    val rest = pad(total % 60)
    return if (hours > 0) "$hours:${pad(minutes)}:$rest" else "$minutes:$rest"
}

private fun pad(value: Long): String = value.toString().padStart(2, '0')
