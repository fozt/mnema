package dev.mnema.app.ui.recordings

import dev.mnema.app.i18n.tr
import dev.mnema.app.model.Lang
import dev.mnema.app.processing.ReprocessOptions
import dev.mnema.app.processing.fmtClock
import dev.mnema.app.processing.isWhole
import java.util.Locale
import kotlin.math.roundToLong

private val WHOLE: String get() = tr("The whole recording is selected")
private val END_BEFORE_START: String get() = tr("The end is before the start")

// unit words in Russian and English
private val UNITS = Regex(
    "^(?:(\\d+)\\s*(?:ч|час\\p{L}*|h))?\\s*(?:(\\d+)\\s*(?:м|мин\\p{L}*|m|min))?\\s*(?:(\\d+)\\s*(?:с|сек\\p{L}*|s|sec))?$",
)

/** Like desktop parse_offset (fixture offsets-v1): "1:05:30", "1:35" are hours:minutes, "1h 35m", "35 min", a bare number is minutes; null when not a time. */
fun parseOffset(text: String): Double? {
    val value = text.trim().lowercase(Locale.ROOT).replace(",", ".")
    if (value.isEmpty()) return null
    UNITS.matchEntire(value)?.takeIf { match -> match.groupValues.drop(1).any { it.isNotEmpty() } }?.let { match ->
        val (h, m, s) = match.groupValues.drop(1).map { it.toLongOrNull() ?: 0L }
        return (h * 3600 + m * 60 + s).toDouble()
    }
    val parts = value.replace(".", ":").split(":")
    if (parts.size > 3 || parts.any { part -> part.isEmpty() || !part.all { it in '0'..'9' } }) return null
    val numbers = parts.map { it.toLongOrNull() ?: return null }
    if (numbers.size == 1) return numbers[0] * 60.0
    val (h, m, s) = numbers + List(3 - numbers.size) { 0L }
    if (m >= 60 || s >= 60) return null
    return (h * 3600 + m * 60 + s).toDouble()
}

sealed interface PartChoice {
    data object Whole : PartChoice
    data class Part(val startS: Double, val endS: Double) : PartChoice
    data class Invalid(val message: String) : PartChoice
}

/** Part of recording fields, like desktop RangePick: the slider sets the fields, a typed time moves the slider; the start is never after the end. */
data class PartForm(
    val length: Double,
    val start: Double = 0.0,
    val end: Double = length,
    val startText: String = fmtClock(start),
    val endText: String = fmtClock(end),
) {
    val whole: Boolean get() = isWhole(start, end, length)

    val caption: String get() = if (whole) WHOLE else tr("Selected {length}", "length" to fmtClock(end - start))

    fun slide(from: Double, to: Double): PartForm {
        val moved = withStart(from.roundToLong().toDouble(), push = true).withEnd(to.roundToLong().toDouble(), push = true)
        return moved.copy(startText = fmtClock(moved.start), endText = fmtClock(moved.end))
    }

    // while typing the other edge stays: "0:5" on the way to "0:52" does not move the end
    fun typeStart(text: String): PartForm {
        val typed = copy(startText = text)
        return parseOffset(text)?.let { typed.withStart(it, push = false) } ?: typed
    }

    fun typeEnd(text: String): PartForm {
        val typed = copy(endText = text)
        return parseOffset(text)?.let { typed.withEnd(it, push = false) } ?: typed
    }

    // left the field: the other edge follows, both fields get the full form
    fun commitStart(): PartForm = parseOffset(startText)?.let { withStart(it, push = true).written() } ?: this

    fun commitEnd(): PartForm = parseOffset(endText)?.let { withEnd(it, push = true).written() } ?: this

    /** What the fields hold, like the desktop dialog answer: an empty start is 0, an empty end is the end of the recording. */
    fun choice(): PartChoice {
        val a = if (startText.isBlank()) 0.0 else parseOffset(startText) ?: return notTime(startText)
        val typedEnd = if (endText.isBlank()) length else parseOffset(endText) ?: return notTime(endText)
        val b = minOf(typedEnd, length)
        return when {
            a >= length -> PartChoice.Invalid(tr("The start {start} is after the end of the recording", "start" to fmtClock(a)))
            b <= a -> PartChoice.Invalid(END_BEFORE_START)
            isWhole(a, b, length) -> PartChoice.Whole
            else -> PartChoice.Part(a, b)
        }
    }

    private fun withStart(value: Double, push: Boolean): PartForm {
        val clamped = value.coerceIn(0.0, length)
        return if (push || clamped <= end) copy(start = clamped, end = maxOf(end, clamped)) else this
    }

    private fun withEnd(value: Double, push: Boolean): PartForm {
        val clamped = value.coerceIn(0.0, length)
        return if (push || clamped >= start) copy(end = clamped, start = minOf(start, clamped)) else this
    }

    private fun written() = copy(startText = fmtClock(start), endText = fmtClock(end))

    private fun notTime(text: String) = PartChoice.Invalid(tr("Could not read the time “{text}”", "text" to text.trim()))
}

/** Choice of the Reprocess sheet; a custom title only if changed, empty means from the content; null when the part is invalid. */
fun reprocessOptions(
    lang: Lang, asr: String, typedTitle: String, currentTitle: String, part: PartChoice, tracks: Set<String>? = null,
): ReprocessOptions? {
    if (tracks != null && tracks.isEmpty()) return null
    val typed = typedTitle.trim()
    val title = typed.takeIf { it.isNotEmpty() && it != currentTitle }
    val whole = ReprocessOptions(lang = lang, asr = asr, title = title, autoName = typed.isEmpty(), tracks = tracks)
    return when (part) {
        is PartChoice.Invalid -> null
        PartChoice.Whole -> whole
        is PartChoice.Part -> whole.copy(startS = part.startS, endS = part.endS)
    }
}

/** The last selected source cannot be turned off. */
fun selectedTracks(selected: Set<String>, label: String): Set<String> = when {
    label !in selected -> selected + label
    selected.size > 1 -> selected - label
    else -> selected
}
