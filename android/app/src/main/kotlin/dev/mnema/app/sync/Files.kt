package dev.mnema.app.sync

import dev.mnema.app.i18n.tr
import dev.mnema.app.recordings.MediaIndex
import java.io.File
import java.io.FileOutputStream
import dev.mnema.app.recordings.freeName
import dev.mnema.app.recordings.sameFolder
import dev.mnema.app.recordings.startedIso
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.charset.CodingErrorAction
import java.nio.file.Files
import java.nio.file.LinkOption.NOFOLLOW_LINKS
import java.nio.file.NoSuchFileException
import java.nio.file.Path
import java.nio.file.StandardCopyOption.ATOMIC_MOVE
import java.nio.file.StandardCopyOption.REPLACE_EXISTING
import java.nio.file.StandardOpenOption.READ
import java.nio.file.attribute.BasicFileAttributes
import java.nio.file.attribute.FileTime
import java.nio.file.attribute.PosixFilePermissions
import java.security.MessageDigest
import java.time.Instant
import java.util.Locale
import java.util.UUID
import java.util.concurrent.TimeUnit.NANOSECONDS
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

internal val filesLock = Any()
data class FileData(val bytes: ByteArray, val stamp: Stamp)

internal fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
internal fun utf8(bytes: ByteArray): String = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString()
internal fun nowNs(): Long = Instant.now().let { Math.addExact(Math.multiplyExact(it.epochSecond, 1_000_000_000L), it.nano.toLong()) }

internal fun safePath(root: File, relative: String): File {
    val parts = relative.split('/')
    if (parts.size !in 1..2 || parts.any { it.isEmpty() || hidden(it) || '\u0000' in it }) throw SyncException(tr("Invalid recording path"))
    return inside(root, File(root, relative))
}

internal fun inside(root: File, file: File): File {
    if (!file.canonicalFile.toPath().startsWith(root.absoluteFile.toPath().normalize())) throw SyncException(tr("The recording path is outside the Mnema folder"))
    return file
}

internal fun recordFile(root: File, path: String, name: FileName): File = inside(root, File(safePath(root, path), name.fileName))
internal fun exists(file: File): Boolean = Files.exists(file.toPath(), NOFOLLOW_LINKS)

internal fun readFile(file: File): FileData {
    fun attributes() = Files.readAttributes(file.toPath(), BasicFileAttributes::class.java)
    val before = attributes()
    if (!before.isRegularFile) throw SyncException(tr("Found a special file instead of a text file"))
    val bytes = Files.readAllBytes(file.toPath())
    val after = attributes()
    if (before.lastModifiedTime() != after.lastModifiedTime() || before.size() != after.size() || before.fileKey() != after.fileKey()) throw SyncException(tr("A file changed during sync"))
    utf8(bytes)
    return FileData(bytes, Stamp(sha256(bytes), after.lastModifiedTime().to(NANOSECONDS)))
}

internal fun stamp(file: File): Stamp? = try { readFile(file).stamp } catch (_: NoSuchFileException) { null }
internal fun syncDirectory(dir: File) { FileChannel.open(dir.toPath(), READ).use { it.force(true) } }

/** A single temporary file next to the target, no staging folder or operation queue. */
internal fun atomicReplace(file: File, bytes: ByteArray, mtimeNs: Long? = null, private: Boolean = false) {
    val target = file.canonicalFile
    val temp = Files.createTempFile(target.parentFile!!.toPath(), ".mnema-sync-", ".tmp")
    try {
        val permissions = if (exists(target)) Files.getPosixFilePermissions(target.toPath())
            else PosixFilePermissions.fromString(if (private) "rw-------" else "rw-r--r--")
        FileOutputStream(temp.toFile()).use { stream ->
            stream.write(bytes)
            Files.setPosixFilePermissions(temp, permissions)
            if (mtimeNs != null) Files.setLastModifiedTime(temp, FileTime.from(mtimeNs, NANOSECONDS))
            stream.fd.sync()
        }
        Files.move(temp, target.toPath(), ATOMIC_MOVE, REPLACE_EXISTING)
        MediaIndex.replaced(target)
        syncDirectory(target.parentFile!!)
    } finally {
        Files.deleteIfExists(temp)
    }
}

internal fun writeFile(file: File, bytes: ByteArray, mtimeNs: Long, expected: Stamp?) = synchronized(filesLock) {
    if (stamp(file) != expected) throw SyncException(tr("The local file has already changed; sync will run again"))
    utf8(bytes)
    atomicReplace(file, bytes, mtimeNs)
}

/** Does not map meta to a DTO, so unknown fields and an existing started stay; without started — the desktop recording_start. */
fun ensureIdentity(folder: File): String = synchronized(filesLock) {
    val file = File(folder, FileName.META.fileName)
    val expected = stamp(file)
    val before = if (expected == null) JsonObject(emptyMap()) else wire.parseToJsonElement(utf8(readFile(file).bytes)).jsonObject
    val data = before.toMutableMap()
    if ("id" in data) metaIdentity(JsonObject(data))
    else data["id"] = JsonPrimitive(UUID.randomUUID().toString())
    if ("started" !in data) startedIso(folder)?.let { data["started"] = JsonPrimitive(it) }
    if (data != before) writeFile(file, (JsonObject(data).toString() + "\n").toByteArray(), nowNs(), expected)
    metaIdentity(JsonObject(data))
}

/** A folder copy becomes a new recording: a new id, the rest of meta as is. */
fun renewIdentity(folder: File): String = synchronized(filesLock) {
    val file = File(folder, FileName.META.fileName)
    val source = readFile(file)
    val data = wire.parseToJsonElement(utf8(source.bytes)).jsonObject.toMutableMap()
    data["id"] = JsonPrimitive(UUID.randomUUID().toString())
    writeFile(file, (JsonObject(data).toString() + "\n").toByteArray(), nowNs(), source.stamp)
    metaIdentity(JsonObject(data))
}

internal fun readRecord(root: File, relative: String): Record {
    val files = mutableMapOf<FileName, Stamp>()
    var id: String? = null
    for (name in FileName.entries) {
        val data = try { readFile(recordFile(root, relative, name)) } catch (e: NoSuchFileException) { if (name == FileName.META) throw e else continue }
        files[name] = data.stamp
        if (name == FileName.META) id = metaIdentity(wire.parseToJsonElement(utf8(data.bytes)).jsonObject)
    }
    return Record(recordingId(id ?: throw SyncException(tr("Could not read the recording metadata"))), relative, files, "")
}

internal enum class Kind { RECORD, SECTION, OTHER }

private val RECORD_FILES = setOf("mic.wav", "system.wav") + FileName.entries.map { it.fileName }
private val TEXTS = setOf(FileName.TRANSCRIPT.fileName, FileName.SUMMARY.fileName)

/** Hidden folders (.obsidian, .git, .Trash-*) are neither sections nor recordings. */
internal fun hidden(name: String) = name.startsWith(".")

/** Temporary name of a recording or section folder during a case change (changeCase): ".mnema-rename-<new name>". */
internal const val RENAMING = ".mnema-rename-"

/** A case change broke off between steps: the hidden recording or section folder returns under its name, " (2)" if taken. */
internal fun restoreRenames(folders: List<File>, busy: Set<String>): Boolean = synchronized(filesLock) {
    var restored = false
    for (dir in folders) {
        val name = dir.name.removePrefix(RENAMING)
        val meta = File(dir, FileName.META.fileName)
        // no meta means a section: the scan cannot see its recordings under the hidden name
        if (!dir.name.startsWith(RENAMING) || name.isEmpty() || hidden(name) || !Files.isDirectory(dir.toPath(), NOFOLLOW_LINKS) ||
            dir.absolutePath in busy || Files.isRegularFile(meta.toPath(), NOFOLLOW_LINKS) && metaId(meta) in busy) continue
        val parent = dir.absoluteFile.parentFile ?: continue
        val target = freeName(File(parent, name))
        try {
            Files.move(dir.toPath(), target.toPath())
            restored = true
        } catch (_: java.io.IOException) { /* the next scan tries again */ }
    }
    restored
}

/**
 * Phone storage is case-insensitive: paths that differ only in case are one folder. Server and last-sync paths are
 * compared with the disk ignoring case, so a pending case change never becomes a deletion or a second copy. The case
 * change itself syncs both ways (LibrarySync), and on disk only through a temporary name (changeCase).
 */
internal fun samePath(a: String, b: String): Boolean = pathKey(a) == pathKey(b)

/** Path key for case-insensitive comparison (samePath). */
internal fun pathKey(path: String): String = path.lowercase(Locale.ROOT)

/** [path] is [folder] or inside it, ignoring case. */
internal fun within(path: String, folder: String): Boolean = samePath(path, folder) || pathKey(path).startsWith(pathKey(folder) + "/")

/** Path [relative] as it is on disk: a section or recording in another case is the same folder. */
internal fun diskPath(root: File, relative: String): String {
    var parent = root
    return relative.split('/').joinToString("/") { part ->
        val names = parent.list().orEmpty()
        val name = if (part in names) part else names.firstOrNull { sameFolder(File(parent, it), File(parent, part)) } ?: part
        parent = File(parent, name)
        name
    }
}

private fun metaId(meta: File): String? = try {
    (wire.parseToJsonElement(utf8(readFile(meta).bytes)).jsonObject["id"] as? JsonPrimitive)?.content
} catch (_: java.io.IOException) { null } catch (_: IllegalArgumentException) { null }

// temporary case-change folders in the root and in sections
private fun renamingFolders(base: File): List<File> {
    fun entries(dir: File) = try { Files.newDirectoryStream(dir.toPath()).use { stream -> stream.map { it.toFile() } } }
        catch (_: java.io.IOException) { emptyList() }
    val top = entries(base)
    val nested = top.filter { !hidden(it.name) && Files.isDirectory(it.toPath(), NOFOLLOW_LINKS) }.flatMap(::entries)
    return (top + nested).filter { it.name.startsWith(RENAMING) }
}

/**
 * Section or recording, by the desktop rule (fixture library-layout-v1); depth 1 is the library root.
 * known — a section of the last sync: recording files in it do not change the layout, meta wins.
 */
internal fun kind(depth: Int, files: Set<String>, folders: List<String>, known: Boolean = false): Kind = when {
    FileName.META.fileName in files -> Kind.RECORD
    depth == 1 && (known || folders.any { !hidden(it) }) -> Kind.SECTION // files next to a section's recordings do not make it a recording
    files.any { it in RECORD_FILES } -> Kind.RECORD
    depth == 1 -> Kind.SECTION
    else -> Kind.OTHER
}

/**
 * Folders sharing an id: the first keeps it — the one at the last sync path [previous], else the one created first, then
 * by [path]; the rest are copies with a new id. One copy rule for the feed, sync and processing.
 */
internal fun <T> keeperFirst(group: List<T>, previous: String?, path: (T) -> String, folder: (T) -> File): List<T> =
    group.sortedWith(compareBy<T> { path(it) != previous }.thenBy { createdNs(folder(it)) }.thenBy(path))

/** When the folder appeared: a copy does not carry file ctimes, so the original's are older. */
internal fun createdNs(folder: File): Long {
    fun ctime(path: Path) = try { (Files.getAttribute(path, "unix:ctime", NOFOLLOW_LINKS) as FileTime).to(NANOSECONDS) }
        catch (_: UnsupportedOperationException) { Long.MAX_VALUE } // no ctime — the path decides
    val files = Files.newDirectoryStream(folder.toPath()).use { it.filter { path -> Files.isRegularFile(path, NOFOLLOW_LINKS) } }
    return files.minOfOrNull(::ctime) ?: ctime(folder.toPath())
}

/** A full disk scan, independent of the card list limit and order. sections and records — the last sync's sections and recording paths. */
fun scan(root: File, busy: Set<String>, sections: Set<String> = emptySet(), records: Set<String> = emptySet()): Scan =
    synchronized(filesLock) {
    val base = root.absoluteFile.normalize()
    val result = mutableListOf<Record>()
    val found = mutableListOf<String>()
    val blocked = mutableListOf<String>()
    val nested = mutableListOf<String>()
    val deep = mutableListOf<String>()
    val withoutFiles = mutableListOf<String>()
    val seen = mutableSetOf<File>()
    // last-sync paths compare ignoring case: phone storage is case-insensitive, a case change is the same folder
    val knownSections = sections.mapTo(HashSet(), ::pathKey)
    val knownRecords = records.mapTo(HashSet(), ::pathKey)
    var complete = true
    fun isDirectory(file: File) = Files.readAttributes(file.toPath(), BasicFileAttributes::class.java).isDirectory
    // hidden entries other than meta are never read
    fun list(dir: File): List<File> = Files.newDirectoryStream(dir.toPath()).use { entries ->
        entries.map { it.toFile() }.filter { !hidden(it.name) || it.name == FileName.META.fileName }.sortedBy { it.name }
    }
    // the scan cannot see a recording in a subfolder; without this its id would count as deleted
    fun inner(folders: List<File>) = folders.firstOrNull { it.canonicalFile.toPath().startsWith(base.toPath()) && File(it, FileName.META.fileName).exists() }
        ?.relativeTo(base)?.invariantSeparatorsPath
    fun visit(folder: File, depth: Int) {
        val relative = folder.relativeTo(base).invariantSeparatorsPath
        try {
            val resolved = safePath(base, relative).canonicalFile
            if (relative in busy || folder.absolutePath in busy) { blocked += relative; return }
            if (!seen.add(resolved)) throw SyncException(tr("The same folder appears twice"))
            val (folders, files) = list(folder).partition(::isDirectory)
            val names = files.map { it.name }.toSet()
            when (kind(depth, names, folders.map { it.name }, known = depth == 1 && pathKey(relative) in knownSections)) {
                Kind.SECTION -> { found += relative; folders.forEach { visit(it, 2) }; return }
                Kind.OTHER -> {
                    // a known recording without its files is not a deletion; an unknown foreign folder is skipped
                    if (pathKey(relative) in knownRecords) { withoutFiles += relative; throw SyncException(tr("The recording folder has none of its files")) }
                    inner(folders)?.let { deep += it; throw SyncException(tr("The recording is in a section subfolder")) }
                    return // an empty or foreign folder in a section: not a recording and no reason to postpone
                }
                Kind.RECORD -> {}
            }
            if (FileName.META.fileName in names) {
                val data = wire.parseToJsonElement(utf8(readFile(recordFile(base, relative, FileName.META)).bytes)).jsonObject
                if ((data["id"] as? JsonPrimitive)?.content in busy) { blocked += relative; return }
            }
            inner(folders)?.let { nested += it; throw SyncException(tr("There is another recording inside the recording")) }
            if (names.any { it in TEXTS } || FileName.META.fileName in names) {
                FileName.entries.forEach { recordFile(base, relative, it) }
                ensureIdentity(folder)
                result += readRecord(base, relative)
            } else blocked += relative // audio without text is not published
        } catch (_: java.io.IOException) { complete = false; blocked += relative }
        catch (_: IllegalArgumentException) { complete = false; blocked += relative }
        catch (_: NoSuchElementException) { complete = false; blocked += relative }
    }
    try { Files.readAttributes(base.toPath(), BasicFileAttributes::class.java) }
    catch (_: NoSuchFileException) {
        return@synchronized if (exists(base)) Scan(complete = false, blockedPaths = listOf("."))
        else Scan(complete = false, missingRoot = true)
    }
    catch (_: java.io.IOException) { return@synchronized Scan(complete = false, blockedPaths = listOf(".")) }
    try {
        if (base.canonicalFile != base) throw SyncException(tr("The Mnema folder was replaced with a link"))
        // a recording under a temporary name is not missing: restore it first; if it stays, the scan is incomplete and its id is kept
        restoreRenames(renamingFolders(base), busy)
        if (renamingFolders(base).any { it.parentFile == base && Files.isDirectory(it.toPath(), NOFOLLOW_LINKS) || Files.isRegularFile(File(it, FileName.META.fileName).toPath(), NOFOLLOW_LINKS) }) complete = false
        list(base).forEach { if (!hidden(it.name) && isDirectory(it)) visit(it, 1) }
    } catch (_: java.io.IOException) { complete = false; blocked += "." }
    Scan(Manifest(result, found), complete, blocked, nested = nested, deep = deep, withoutFiles = withoutFiles)
}

internal fun metaIdentity(data: JsonObject): String {
    val value = data["id"] as? JsonPrimitive
    return recordingId(value?.takeIf { it.isString }?.content ?: throw SyncException(tr("Could not read the recording id")))
}
