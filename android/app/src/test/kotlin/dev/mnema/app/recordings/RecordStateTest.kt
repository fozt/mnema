package dev.mnema.app.recordings

import dev.mnema.app.sync.resource
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import org.junit.Assert.assertEquals
import org.junit.Test

/** Fixture shared with the desktop record-state-v1 (reference: mnema/live/meta.record_state). */
class RecordStateTest {
    @Test
    fun filesHistoryAndWorkGiveTheSharedState() {
        val cases = Json.parseToJsonElement(resource("record-state-v1.json")).jsonObject.getValue("cases").jsonArray
        for (case in cases.map { it.jsonObject }) {
            val name = case.getValue("name").jsonPrimitive.content
            val sizes = case.getValue("files").jsonObject.mapValues { it.value.jsonPrimitive.long }
            val meta = JsonObject(mapOf("runs" to case.getValue("runs")))
            val work = Work.valueOf(case.getValue("work").jsonPrimitive.content.uppercase())
            assertEquals(name, RecordState.valueOf(case.getValue("state").jsonPrimitive.content.uppercase()), recordState(sizes, meta, work))
        }
    }
}
