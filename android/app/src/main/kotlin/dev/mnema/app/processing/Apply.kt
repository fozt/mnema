package dev.mnema.app.processing

import dev.mnema.app.Redact
import dev.mnema.app.i18n.tr
import dev.mnema.app.recordings.displayTitle
import dev.mnema.app.recordings.freeName
import dev.mnema.app.recordings.moveFolder
import dev.mnema.app.sync.filesLock
import dev.mnema.app.server.RecordingResult
import dev.mnema.app.server.validFolderName
import dev.mnema.app.transcript.Meta
import dev.mnema.app.transcript.RecordingFolder
import dev.mnema.app.transcript.atomicWrite
import java.io.File
import java.io.IOException
import java.nio.file.Files
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive


private const val JOURNAL = ".mnema-apply.json"
private val journalJson = Json { encodeDefaults = true; ignoreUnknownKeys = true }

@Serializable
private data class ApplyJournal(val job: String, val from: String, val target: String?)

/** Remark code: Android did not let the recording folder be named; "Summarize" does not repeat it — the title is already there. */
internal const val TITLE_NOT_APPLIED = "title_not_applied"

// no hint: Android would refuse renaming this folder the same way
private val NOT_RENAMED: String get() = tr("Android didn't allow renaming the folder")

/**
 * History is the last entry; the journal survives an interrupted rename together with the folder. Returns the recording folder after it.
 * A taken result name becomes the next free one; [summaryOnly] — a summary result.
 * When renaming the folder is refused ([move] throws IOException, the folder stays), the result stays in the old folder with a remark.
 */
fun applyResult(
    dir: File, answer: RecordingResult, job: String, server: String?, sentName: String, recordingNow: String?, summaryOnly: Boolean,
    move: (File, File) -> Unit = ::moveFolder,
    log: (String) -> Unit = {},
): File = synchronized(filesLock) {
    if (!dir.isDirectory || Files.isSymbolicLink(dir.toPath())) throw IOException(tr("The recording folder is unavailable"))
    val result = answer.checked(summaryOnly)
    var journal = readJournal(dir)?.takeIf { it.job == job }
    if (Meta.hasJob(dir, job)) return@synchronized dir
    if (journal == null) {
        val target = result.folder?.takeIf { dir.name == sentName && dir.name != recordingNow }
            ?.let { freeName(File(dir.parentFile, it), dir).name }
        journal = ApplyJournal(job, sentName, target)
        saveJournal(dir, journal)
    }
    RecordingFolder.migrateSummary(dir) // the new summary replaces the old file
    for ((name, content) in result.files) {
        val file = File(dir, name)
        if (Files.isSymbolicLink(file.toPath()) || file.isDirectory) throw IOException(tr("The recording file is unavailable"))
        if (content == null) Files.deleteIfExists(file.toPath())
        else atomicWrite(file, content.toByteArray(Charsets.UTF_8))
    }
    var current = dir
    var notRenamed: String? = null
    if (journal.target != null && dir.name == journal.from && dir.name != recordingNow) {
        val target = freeName(File(dir.parentFile, journal.target), dir)
        if (target.name != journal.target) {
            journal = journal.copy(target = target.name)
            saveJournal(dir, journal)
        }
        current = target
        if (target.name != dir.name) {
            try {
                move(dir, target)
            } catch (e: IOException) {
                // the folder is gone — a retry finds it by id; still in place — MediaProvider refused (EPERM), a retry won't help.
                if (!dir.isDirectory) throw e
                log("Recording folder not renamed: ${Redact.redact(e.message.orEmpty())}")
                notRenamed = target.name
                current = dir
            }
        }
    }
    val settings = (result.run["settings"] as? JsonObject).orEmpty() + ("server" to JsonPrimitive(server))
    var run = JsonObject(result.run + mapOf("job" to JsonPrimitive(job), "settings" to JsonObject(settings)))
    if (notRenamed != null) run = withRemark(run, notApplied(notRenamed))
    Meta.appendRun(current, run, strict = true)
    current
}

/** A remark shaped like Remark in live/remarks.py: the title step, the text is the cause; written in the interface language. */
private fun notApplied(target: String): JsonObject {
    val cause = displayTitle(target).title?.let { tr("Title “{title}” not applied: Android didn't allow renaming the folder", "title" to it) }
        ?: tr("Title not applied: Android didn't allow renaming the folder")
    return JsonObject(mapOf(
        "step" to JsonPrimitive("title"), "cause" to JsonPrimitive(cause),
        "short" to JsonPrimitive(NOT_RENAMED), "short_effect" to JsonPrimitive(tr("no title")),
        "code" to JsonPrimitive(TITLE_NOT_APPLIED),
    ))
}

private fun withRemark(run: JsonObject, note: JsonObject): JsonObject {
    fun list(key: String): List<JsonElement> = (run[key] as? JsonArray).orEmpty()
    val text = "${(note.getValue("cause") as JsonPrimitive).content}."
    return JsonObject(run + mapOf(
        "notes" to JsonArray(list("notes") + note),
        "remarks" to JsonArray(list("remarks") + JsonPrimitive(text)),
    ))
}

private fun readJournal(dir: File): ApplyJournal? {
    val file = File(dir, JOURNAL)
    if (Files.isSymbolicLink(file.toPath())) throw IOException(tr("The recording journal is unavailable"))
    if (!file.exists()) return null
    return try {
        journalJson.decodeFromString(ApplyJournal.serializer(), file.readText(Charsets.UTF_8)).also {
            if (it.target != null && !validFolderName(it.target)) throw IOException(tr("The recording journal has an invalid name"))
        }
    } catch (e: IllegalArgumentException) {
        throw IOException(tr("The result journal can't be read"), e)
    }
}

private fun saveJournal(dir: File, journal: ApplyJournal) =
    atomicWrite(File(dir, JOURNAL), journalJson.encodeToString(ApplyJournal.serializer(), journal).toByteArray(Charsets.UTF_8))
