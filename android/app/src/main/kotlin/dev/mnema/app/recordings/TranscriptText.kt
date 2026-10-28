package dev.mnema.app.recordings

import dev.mnema.app.model.DialogLine
import dev.mnema.app.model.Summary

// paragraph label of the desktop with_speakers: a track, or "Спикер ?" / "Speaker ?" for an unlabeled track (fixture transcript-v1)
private val DIALOG_LABEL = Regex("^(?:\\[([0-9]+:[0-9]{2}(?::[0-9]{2})?)\\]\\s+)?(mic|system|(?:Спикер|Speaker) \\?):(?:\\s(.*))?$")
private val TIMESTAMP = Regex("^\\[([0-9]+:[0-9]{2}(?::[0-9]{2})?)\\]\\s+(.*)$", RegexOption.DOT_MATCHES_ALL)
private val TRACKS = setOf("mic", "system")
private val PARAGRAPH = Regex("\\n\\s*\\n")

private fun seconds(text: String): Double? {
    val numbers = text.split(':').map { it.toDoubleOrNull() ?: return null }
    if (numbers.size !in 2..3 || numbers.takeLast(2).any { it >= 60 } || numbers.any { !it.isFinite() }) return null
    return numbers.fold(0.0) { total, next -> total * 60 + next }.takeIf { it.isFinite() }
}

/** Paragraphs of transcript.txt (desktop with_speakers, fixture transcript-v1) and lines without a timestamp. */
fun parseDialog(text: String): List<DialogLine> {
    val lines = mutableListOf<DialogLine>()
    for (raw in text.lines()) {
        val label = DIALOG_LABEL.matchEntire(raw)
        // a timestamped paragraph without a track label starts its own paragraph
        val stamped = if (label == null) TIMESTAMP.matchEntire(raw) else null
        when {
            label != null -> lines += DialogLine(label.groupValues[2].takeIf { it in TRACKS }.orEmpty(), label.groupValues[3], seconds(label.groupValues[1]))
            stamped != null -> lines += DialogLine("", stamped.groupValues[2], seconds(stamped.groupValues[1]))
            raw.isBlank() || lines.isEmpty() -> Unit
            else -> lines[lines.lastIndex] = lines.last().let { it.copy(text = it.text + "\n" + raw) }
        }
    }
    return lines.map { it.copy(text = it.text.trim()) }.filter { it.text.isNotEmpty() }
}

/** Gist and points of `summary.txt` (fixture summary-v1, like the desktop _gist). */
fun parseSummaryFile(text: String): Summary {
    val lines = text.lines()
    val bullets = mutableListOf<StringBuilder>()
    val bodyLines = mutableListOf<String>()
    var current: StringBuilder? = null
    for (line in lines) {
        when {
            line.startsWith("- ") -> {
                current = StringBuilder(line.removePrefix("- ").trim())
                bullets += current
            }
            current != null && line.isNotBlank() -> current.append("\n").append(line.trim())
            else -> { current = null; bodyLines += line }
        }
    }
    val points = bullets.map { it.toString().trim() }.filter { it.isNotEmpty() }.map { point ->
        val mark = TIMESTAMP.matchEntire(point)
        val at = mark?.groupValues?.get(1)?.let(::seconds)
        if (at == null) point to null else mark.groupValues[2] to at
    }
    val body = bodyLines.joinToString("\n")
    val about = PARAGRAPH.split(body).map { it.trim() }.firstOrNull { it.isNotEmpty() && !it.startsWith("- ") && !it.startsWith("* ") }
    return Summary(about.orEmpty(), points.map { it.first }, points.map { it.second })
}
