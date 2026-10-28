package dev.mnema.app.recordings

import dev.mnema.app.transcript.Meta
import dev.mnema.app.transcript.Wav
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.LinkOption.NOFOLLOW_LINKS
import java.nio.file.attribute.BasicFileAttributes
import java.time.DateTimeException
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.util.Locale
import java.util.concurrent.TimeUnit.NANOSECONDS

// Port of mnema/live/naming.py: a recording's title is its folder name without the leading date.
// Fixtures shared with the desktop: folder-titles-v1, folder-names-v1, recording-start-v1.

// [0-9], not \d: ICU on the phone treats "２" as a digit too, like Python without re.ASCII
private val UNNAMED = Regex("([0-9]{4})-([0-9]{2})-([0-9]{2})_([0-9]{2})-([0-9]{2})(?: \\([0-9]+\\))?")
private val NAMED = Regex("^([0-9]{4})-([0-9]{2})-([0-9]{2})_(.+?)(?: \\([0-9]+\\))?$")
private val DATED = Regex("^([0-9]{4})-([0-9]{2})-([0-9]{2})(?:$|_)")
private val PART = Regex("( \\([0-9]+(?:\\.[0-9]{2}){1,2}–[0-9]+(?:\\.[0-9]{2}){1,2}\\))(?: \\([0-9]+\\))?$")
private val UNSAFE = Regex("[\\x00-\\x1f\\x7f-\\x9f/\\\\:*?\"<>|]")
private val SPACES = Regex("[\\s\\p{Z}]+")
private const val MAX_TITLE = 80
// room for " (2)" below the file system limit
private const val MAX_NAME_BYTES = 240
// file system limit for a folder name
internal const val NAME_MAX_BYTES = 255

/** Title from a folder name; [title] null — a start-time name, the time is shown; [part] — its part mark. */
data class FolderTitle(val title: String?, val part: String = "")

/** Recording start in the zone where it was recorded; [known] false — only the date is known. */
data class RecordingStart(val at: LocalDateTime, val known: Boolean)

/** Desktop part_label: the part mark " (0.10–1.35)" at the end of the name, ignoring a " (2)" suffix; empty when absent. */
fun partMark(name: String): String = PART.find(name)?.groupValues?.get(1).orEmpty()

private fun offset(seconds: Double): String {
    val total = seconds.toLong()
    val (h, m, s) = Triple(total / 3600, total % 3600 / 60, total % 60)
    return if (s == 0L) String.format(Locale.ROOT, "%d.%02d", h, m) else String.format(Locale.ROOT, "%d.%02d.%02d", h, m, s)
}

/** Desktop part_of: a part mark from its bounds in seconds, "0.10–1.35", with seconds "0.10.05–…". */
fun partOf(startS: Double, endS: Double): String = "${offset(startS)}–${offset(endS)}"

private fun unparted(name: String): String {
    var plain = name
    while (PART.containsMatchIn(plain)) plain = PART.replace(plain, "")
    return plain
}

private fun byTime(name: String): MatchResult? = UNNAMED.matchEntire(unparted(name))

/** Desktop display_title: the leading date is dropped; a start-time name has no title. */
fun displayTitle(name: String): FolderTitle {
    if (byTime(name) != null) return FolderTitle(null, partMark(name))
    NAMED.matchEntire(name)?.groupValues?.get(4)?.trim()?.takeIf { it.isNotEmpty() }?.let { return FolderTitle(it) }
    return FolderTitle(name)
}

private fun codePoints(text: String) = text.codePointCount(0, text.length)

private fun dropLast(text: String) = text.substring(0, text.offsetByCodePoints(text.length, -1))

/** Desktop folder_title: forbidden and control characters become spaces, spaces collapse, edge dots and spaces are trimmed, up to 80 characters. */
internal fun folderTitle(title: String): String {
    var text = SPACES.replace(UNSAFE.replace(title, " "), " ").trim(' ', '.')
    if (codePoints(text) > MAX_TITLE) text = text.substring(0, text.offsetByCodePoints(0, MAX_TITLE))
    return text.trimEnd(' ', '.')
}

// the title as typed: spaces collapsed, a part mark typed at the end ignored
private fun typed(title: String, part: String): String {
    val text = SPACES.replace(title, " ").trim()
    return if (part.isEmpty()) text else text.removeSuffix(part.trim()).trimEnd()
}

/**
 * Desktop chosen_title: the user's title (processing dialog, "Set title…"), ignoring part mark [part] at the end; null
 * when nothing is left for a folder name ("???", "...", only the mark) and processing names the folder (folder-names-v1, own).
 */
fun chosenTitle(text: String?, part: String = ""): String? = typed(text.orEmpty(), part).takeIf { folderTitle(it).isNotEmpty() }

/** Desktop folder_name: "2026-10-21_Team standup" with the part mark, "2026-10-21_08-35" without a title; at most 240 bytes. */
fun folderName(start: LocalDateTime, title: String, part: String = ""): String {
    val day = "${start.toLocalDate()}_"
    val name = folderTitle(typed(title, part))
        .ifEmpty { String.format(Locale.ROOT, "%02d-%02d", start.hour, start.minute) }
    return day + withinBytes(name, "$day$part".toByteArray(Charsets.UTF_8).size) + part
}

/** A title without a date (a section name), also at most 240 bytes. */
internal fun sectionTitle(title: String): String = withinBytes(folderTitle(title), 0)

// the title is cut from the end until, with [reserved] bytes of date and mark, it fits in 240 bytes
private fun withinBytes(title: String, reserved: Int): String {
    var name = title
    while (codePoints(name) > 1 && name.toByteArray(Charsets.UTF_8).size + reserved > MAX_NAME_BYTES) {
        name = dropLast(name).trimEnd(' ', '.')
    }
    return name
}

/** The same folder, also under a name in another letter case; links are not followed. */
internal fun sameFolder(path: File, own: File?): Boolean = own != null && try {
    !Files.isSymbolicLink(path.toPath()) && Files.isSameFile(path.toPath(), own.toPath())
} catch (_: IOException) {
    false
}

/** Desktop free_name: a name taken by another recording gets " (2)", " (3)", at most 255 bytes; the own folder does not count as taken. */
fun freeName(target: File, own: File? = null): File {
    var candidate = target
    var n = 2
    while (Files.exists(candidate.toPath(), NOFOLLOW_LINKS) && !sameFolder(candidate, own)) {
        val suffix = " (${n++})"
        var stem = target.name
        while ((stem + suffix).toByteArray(Charsets.UTF_8).size > NAME_MAX_BYTES && codePoints(stem) > 1) stem = dropLast(stem)
        candidate = File(target.parentFile, stem + suffix)
    }
    return candidate
}

private fun namedStart(name: String): RecordingStart? = try {
    byTime(name)?.let { match ->
        val (year, month, day, hour, minute) = match.destructured
        RecordingStart(LocalDateTime.of(year.toInt(), month.toInt(), day.toInt(), hour.toInt(), minute.toInt()), known = true)
    } ?: DATED.find(name)?.let { match ->
        val (year, month, day) = match.destructured
        RecordingStart(LocalDate.of(year.toInt(), month.toInt(), day.toInt()).atStartOfDay(), known = false)
    }
} catch (_: DateTimeException) { null } // "2026-13-45_…" is not a date

private fun metaStart(dir: File): LocalDateTime? = Meta.started(dir)?.let { value ->
    try { OffsetDateTime.parse(value).toLocalDateTime() } catch (_: DateTimeException) {
        try { LocalDateTime.parse(value) } catch (_: DateTimeException) { null }
    }
}

// From the recording files (hidden ".…" ones ignored): the longest WAV track — its end minus its length; otherwise the earliest audio, otherwise the earliest file.
private fun trackStart(dir: File, zone: ZoneId): LocalDateTime? {
    val files = try {
        Files.newDirectoryStream(dir.toPath()).use { entries ->
            entries.mapNotNull { path ->
                val name = path.fileName.toString()
                // the file vanished during the scan
                val attributes = try { Files.readAttributes(path, BasicFileAttributes::class.java, NOFOLLOW_LINKS) } catch (_: IOException) { null }
                attributes?.let { name to it }?.takeIf { it.second.isRegularFile && !name.startsWith(".") }
            }
        }
    } catch (_: IOException) { return null } // the folder is missing or unreadable: the start comes from the name
    val longest = files.filter { it.first.lowercase().endsWith(".wav") }
        .map { (name, attributes) -> Wav.seconds(File(dir, name)) to attributes.lastModifiedTime().to(NANOSECONDS) }
        .maxWithOrNull(compareBy({ it.first }, { it.second }))
    val instant = if (longest != null) {
        Instant.EPOCH.plusNanos(longest.second - (longest.first * 1_000_000_000L).toLong())
    } else {
        val audio = files.filter { File(it.first).extension.lowercase() in AUDIO_EXTENSIONS }
        audio.ifEmpty { files }.minOfOrNull { it.second.lastModifiedTime().toInstant() } ?: return null
    }
    return LocalDateTime.ofInstant(instant, zone)
}

private fun start(dir: File, zone: ZoneId): RecordingStart? {
    val named = namedStart(dir.name)
    if (named != null && named.known) return named
    val at = metaStart(dir) ?: trackStart(dir, zone) ?: return named
    if (named != null && named.at.toLocalDate() != at.toLocalDate()) return RecordingStart(LocalDateTime.of(named.at.toLocalDate(), at.toLocalTime()), known = true)
    return RecordingStart(at, known = true)
}

/**
 * Desktop recording_start: a start-time name gives the date and time; otherwise meta started or a track, with the leading
 * date of the name winning when the day differs (the folder was copied, mtime rewritten, the date fixed by hand). The
 * folder mtime is never used; when nothing is known — now.
 */
fun recordingStart(dir: File, zone: ZoneId = ZoneId.systemDefault()): RecordingStart =
    start(dir, zone) ?: RecordingStart(LocalDateTime.now(zone), known = false)

/** Desktop started_iso: meta started for a recording without one, by the same rule in [zone]; null when the start time is unknown, a guess is never written. */
fun startedIso(dir: File, zone: ZoneId = ZoneId.systemDefault()): String? =
    start(dir, zone)?.takeIf { it.known }?.let { Meta.iso(it.at.atZone(zone)) }
