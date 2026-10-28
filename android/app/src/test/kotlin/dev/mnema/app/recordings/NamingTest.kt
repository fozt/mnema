package dev.mnema.app.recordings

import dev.mnema.app.sync.resource
import dev.mnema.app.transcript.Wav
import java.io.File
import java.io.RandomAccessFile
import java.nio.file.Files
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.double
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.long
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** Fixtures shared with the desktop: folder-titles-v1, folder-names-v1, recording-start-v1 (reference: mnema/live/naming.py). */
class NamingTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private fun fixture(name: String): JsonObject = Json.parseToJsonElement(resource(name)).jsonObject
    private fun JsonObject.text(key: String): String? = get(key)?.jsonPrimitive?.contentOrNull

    private fun meta(dir: File, started: String?) {
        val fields = listOfNotNull(""""version":1""", """"runs":[]""", started?.let { """"started":${JsonPrimitive(it)}""" })
        File(dir, ".mnema-meta.json").writeText(fields.joinToString(",", "{", "}"))
    }

    @Test
    fun folderNameGivesTheSharedTitleAndStart() {
        for (case in fixture("folder-titles-v1.json").getValue("cases").jsonArray.map { it.jsonObject }) {
            val name = case.text("folder")!!
            val shown = displayTitle(name)
            assertEquals(name, case.text("title"), shown.title)
            assertEquals(name, case.text("part")?.let { " ($it)" }.orEmpty(), shown.part)
            val start = case.text("start") ?: continue
            val found = recordingStart(File(tmp.newFolder(), name).apply { mkdir() })
            if ('T' in start) assertEquals(name, RecordingStart(LocalDateTime.parse(start), known = true), found)
            else assertEquals(name, LocalDate.parse(start) to false, found.at.toLocalDate() to found.known)
        }
    }

    @Test
    fun titleGivesTheSharedFolderName() {
        for (case in fixture("folder-names-v1.json").getValue("names").jsonArray.map { it.jsonObject }) {
            val root = tmp.newFolder()
            val taken = case["taken"]?.jsonArray?.map { it.jsonPrimitive.content }.orEmpty()
            taken.forEach { File(root, it).mkdir() }
            val dir = File(root, case.text("current")!!).apply { mkdir() }
            File(dir, "mic.wav").writeText("audio")
            meta(dir, case.text("started"))
            val expected = case.text("name")!!
            val renamed = renameRecording(dir, case.text("title")!!)
            assertEquals(expected, renamed.name)
            assertEquals(expected, "audio", File(renamed, "mic.wav").readText())
            assertEquals(expected, (taken + expected).toSet(), root.list()!!.toSet())
        }
    }

    @Test
    fun takenNameGivesTheSharedFreeName() {
        for (case in fixture("folder-names-v1.json").getValue("free").jsonArray.map { it.jsonObject }) {
            val root = tmp.newFolder()
            case.getValue("taken").jsonArray.forEach { File(root, it.jsonPrimitive.content).mkdir() }
            assertEquals(case.text("free"), freeName(File(root, case.text("name")!!)).name)
        }
    }

    @Test
    fun ownFolderAndALinkAreNotFreeTheSameWay() {
        val own = tmp.newFolder("2026-10-21_Дейли")
        assertEquals(own, freeName(File(tmp.root, "2026-10-21_Дейли"), own))
        val link = File(tmp.root, "2026-10-21_Планёрка")
        Files.createSymbolicLink(link.toPath(), own.toPath())
        assertEquals("2026-10-21_Планёрка (2)", freeName(link, own).name)
    }

    @Test
    fun partBoundsGiveTheSharedLabel() {
        for (case in fixture("folder-names-v1.json").getValue("parts").jsonArray.map { it.jsonObject }) {
            assertEquals(case.text("label"), partOf(case.getValue("start").jsonPrimitive.double, case.getValue("end").jsonPrimitive.double))
        }
    }

    @Test
    fun folderGivesTheSharedRecordingStart() {
        val table = fixture("recording-start-v1.json")
        val zone = ZoneId.of(table.text("zone")!!)
        for (case in table.getValue("cases").jsonArray.map { it.jsonObject }) {
            val name = case.text("folder")!!
            val dir = File(tmp.newFolder(), name).apply { mkdir() }
            if ("meta" in case) meta(dir, case.text("meta"))
            for (file in case["files"]?.jsonArray.orEmpty().map { it.jsonObject }) {
                val path = File(dir, file.text("name")!!)
                RandomAccessFile(path, "rw").use { it.setLength(file.getValue("size").jsonPrimitive.long) }
                path.setLastModified(Instant.parse(file.text("mtime")!!).toEpochMilli())
            }
            val found = recordingStart(dir, zone)
            assertEquals(name, case.getValue("known").jsonPrimitive.boolean, found.known)
            case.text("start")?.let { assertEquals(name, LocalDateTime.parse(it), found.at) }
            val stored = case["stored"]?.jsonPrimitive?.boolean ?: true
            assertEquals(name, stored, startedIso(dir, zone) != null)
        }
    }

    @Test
    fun typedOwnTitleGivesTheSharedOne() {
        for (case in fixture("folder-names-v1.json").getValue("own").jsonArray.map { it.jsonObject }) {
            assertEquals(case.toString(), case.text("own"), chosenTitle(case.text("typed"), case.text("part")!!))
        }
    }

    @Test
    fun startGivesTheSharedNameByTime() {
        for (case in fixture("folder-names-v1.json").getValue("by_time").jsonArray.map { it.jsonObject }) {
            assertEquals(case.text("name"), folderName(LocalDateTime.parse(case.text("start")!!), ""))
        }
    }

    // a track file from the fixture's lengths case: no header (size) or a 16-bit PCM WAV (wav)
    private fun track(file: File, case: JsonObject) {
        case["size"]?.let { size -> RandomAccessFile(file, "rw").use { it.setLength(size.jsonPrimitive.long) }; return }
        val wav = case.getValue("wav").jsonObject
        fun int(key: String, default: Long? = null) = wav[key]?.jsonPrimitive?.long ?: default!!
        val rate = int("rate")
        val block = int("channels", 1) * 2
        val data = int("frames") * block
        val out = java.io.ByteArrayOutputStream()
        fun u32(value: Long) = out.write(java.nio.ByteBuffer.allocate(4).order(java.nio.ByteOrder.LITTLE_ENDIAN).putInt(value.toInt()).array())
        fun u16(value: Long) = out.write(java.nio.ByteBuffer.allocate(2).order(java.nio.ByteOrder.LITTLE_ENDIAN).putShort(value.toShort()).array())
        fun chunk(name: String, size: Long, body: Long) { out.write(name.toByteArray()); u32(size); out.write(ByteArray(body.toInt())) }
        out.write("fmt ".toByteArray()); u32(16); u16(1); u16(block / 2); u32(rate); u32(rate * block); u16(block); u16(16)
        wav["before"]?.let { chunk("LIST", it.jsonPrimitive.long, it.jsonPrimitive.long) }
        chunk("data", int("declared", data), data)
        wav["after"]?.let { chunk("LIST", it.jsonPrimitive.long, it.jsonPrimitive.long) }
        val body = out.toByteArray()
        file.writeBytes("RIFF".toByteArray() + java.nio.ByteBuffer.allocate(4).order(java.nio.ByteOrder.LITTLE_ENDIAN)
            .putInt(body.size + 4).array() + "WAVE".toByteArray() + body)
    }

    @Test
    fun wavTrackGivesTheSharedLength() {
        for (case in fixture("recording-start-v1.json").getValue("lengths").jsonArray.map { it.jsonObject }) {
            val file = File(tmp.newFolder(), "mic.wav")
            track(file, case)
            assertEquals(case.toString(), case.getValue("seconds").jsonPrimitive.double, Wav.seconds(file), 0.0)
        }
    }
}
