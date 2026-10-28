package dev.mnema.app.transcript

import dev.mnema.app.sync.resource
import java.io.File
import java.io.IOException
import java.util.TimeZone
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.assertThrows
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class MetaTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private fun run(kind: String, note: String = "Привет"): JsonObject = buildJsonObject {
        put("kind", kind)
        put("status", "ok")
        put("note", note)
    }

    private fun meta(dir: File = tmp.root) = File(dir, ".mnema-meta.json")

    /** Fixture shared with the desktop shown-run-v1: which run the remarks are about and which one a summary run takes as previous. */
    @Test
    fun remarksAndSummaryTakeTheLastRunThatWasNotStopped() {
        val cases = Json.parseToJsonElement(resource("shown-run-v1.json")).jsonObject.getValue("cases").jsonArray
        for (case in cases.map { it.jsonObject }) {
            val runs = case.getValue("runs").jsonArray
            val shown = case.getValue("shown").takeUnless { it is JsonNull }?.jsonPrimitive?.int
            assertEquals(case.getValue("name").jsonPrimitive.content, shown?.let { runs[it] }, Meta.lastRun(JsonObject(mapOf("runs" to runs))))
        }
    }

    @Test
    fun recordingIdentityAndUnknownFieldsSurviveEveryUpdate() {
        meta().writeText("""{"version":1,"future":{"keep":true},"runs":[]}""")
        val zone = TimeZone.getTimeZone("Europe/Moscow")
        Meta.ensureRecording(tmp.root, "id-one") { Meta.isoNow(1_760_000_000_123L, zone) }
        val started = Meta.started(tmp.root)
        Meta.ensureRecording(tmp.root, "id-two") { Meta.isoNow(0, TimeZone.getTimeZone("UTC")) }
        val event = buildJsonObject { put("job", "job"); put("status", "ok") }
        Meta.appendRun(tmp.root, event, strict = true)
        Meta.appendRun(tmp.root, event, strict = true)
        val data = requireNotNull(Meta.read(tmp.root))
        assertEquals(JsonPrimitive("id-one"), data["id"])
        assertEquals("2025-10-09T11:53:20+03:00", started)
        assertEquals(started, Meta.started(tmp.root))
        assertEquals(Json.parseToJsonElement("""{"keep":true}"""), data["future"])
        assertEquals(1, data["runs"]!!.jsonArray.size)
        assertEquals(event, Meta.lastRun(tmp.root))
        assertTrue(Meta.hasJob(tmp.root, "job"))
    }

    @Test
    fun requiredHistoryDoesNotSilentlyAcceptWriteFailureOrANewerFormat() {
        assertThrows(IOException::class.java) { Meta.appendRun(File(tmp.root, "missing"), run("process"), strict = true) }
        // an optional history write stays silent and processing does not fail because of it
        Meta.appendRun(File(tmp.root, "missing"), run("process"))
        meta().writeText("""{"version":2,"id":"preserve","runs":[]}""")
        assertThrows(IOException::class.java) { Meta.appendRun(tmp.root, run("process"), strict = true) }
        assertEquals("""{"version":2,"id":"preserve","runs":[]}""", meta().readText())
    }

    @Test
    fun firstRunCreatesTheFile() {
        val first = run("process")
        val second = run("summary")

        Meta.appendRun(tmp.root, first)

        val parsed = Json.parseToJsonElement(meta().readText()).jsonObject
        assertEquals(setOf("version", "runs"), parsed.keys)
        assertEquals(1, parsed.getValue("version").jsonPrimitive.int)
        assertEquals(JsonArray(listOf(first)), parsed.getValue("runs"))

        Meta.appendRun(tmp.root, second)

        val runs = Json.parseToJsonElement(meta().readText()).jsonObject.getValue("runs").jsonArray
        assertEquals("the next run is appended", listOf(first, second), runs.toList())
    }

    @Test
    fun historyOfANewerVersionIsNotTouched() {
        val newer = """{"version": 2, "runs": [{"x": 1}]}"""
        meta().writeText(newer)

        Meta.appendRun(tmp.root, run("process"))

        assertEquals(newer, meta().readText())
        assertFalse(File(tmp.root, ".mnema-meta.json.bad").exists())
    }

    @Test
    fun unreadableFileIsKeptAsBadAndHistoryStartsAgain() {
        for ((index, broken) in listOf("это не json {", """{"version": 1, "runs": null}""").withIndex()) {
            val dir = tmp.newFolder("case$index")
            meta(dir).writeText(broken)

            Meta.appendRun(dir, run("process"))

            assertEquals(broken, File(dir, ".mnema-meta.json.bad").readText())
            assertEquals(broken, 1, Meta.read(dir)!!.getValue("runs").jsonArray.size)
        }
    }

    @Test
    fun objectWithoutRunsGetsThem() {
        meta().writeText("{}")

        Meta.appendRun(tmp.root, run("process"))

        assertFalse(File(tmp.root, ".mnema-meta.json.bad").exists())
        assertEquals(1, Meta.read(tmp.root)!!.getValue("runs").jsonArray.size)
    }

    @Test
    fun readGivesTheParsedObjectOrNull() {
        assertNull(Meta.read(tmp.root))
        meta().writeText("[1, 2]")
        assertNull(Meta.read(tmp.root))
        meta().writeText("{")
        assertNull(Meta.read(tmp.root))

        meta().writeText("""{"version": 1, "runs": []}""")
        assertEquals(JsonPrimitive(1), Meta.read(tmp.root)!!.getValue("version"))
    }

    @Test
    fun noTemporaryFilesAreLeftBehind() {
        Meta.appendRun(tmp.root, run("process"))
        Meta.appendRun(tmp.root, run("summary"))

        assertEquals(setOf(".mnema-meta.json"), tmp.root.list()!!.toSet())
    }

    @Test
    fun timeIsIsoWithTheOffsetAndWholeSeconds() {
        val moment = 1_760_000_000_123L

        assertEquals("2025-10-09T08:53:20+00:00", Meta.isoNow(moment, TimeZone.getTimeZone("UTC")))
        assertEquals("2025-10-09T11:53:20+03:00", Meta.isoNow(moment, TimeZone.getTimeZone("Europe/Moscow")))
        assertEquals("2025-10-09T14:23:20+05:30", Meta.isoNow(moment, TimeZone.getTimeZone("Asia/Kolkata")))
    }
}
