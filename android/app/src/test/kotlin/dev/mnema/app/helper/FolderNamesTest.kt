package dev.mnema.app.helper

import dev.mnema.app.sync.resource
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.ZonedDateTime
import java.util.TimeZone

class FolderNamesTest {
    @get:Rule
    val folder = TemporaryFolder()

    @Test
    fun defaultNameIsLocalMinuteWithoutSeconds() {
        val moscow = ZoneId.of("Europe/Moscow")
        val epochMs = ZonedDateTime.of(2026, 10, 21, 14, 35, 33, 0, moscow).toInstant().toEpochMilli()

        assertEquals("2026-10-21_14-35", FolderNames.defaultName(epochMs, TimeZone.getTimeZone(moscow)))
        assertEquals("2026-10-21_11-35", FolderNames.defaultName(epochMs, TimeZone.getTimeZone("UTC")))
    }

    @Test
    fun startGivesTheSharedNameByTime() {
        val utc = TimeZone.getTimeZone("UTC")
        for (case in Json.parseToJsonElement(resource("folder-names-v1.json")).jsonObject.getValue("by_time").jsonArray.map { it.jsonObject }) {
            val epochMs = LocalDateTime.parse(case.getValue("start").jsonPrimitive.content).toInstant(ZoneOffset.UTC).toEpochMilli()
            assertEquals(case.getValue("name").jsonPrimitive.content, FolderNames.defaultName(epochMs, utc))
        }
    }

    @Test
    fun takenNameGivesTheSharedFreeName() {
        val cases = Json.parseToJsonElement(resource("folder-names-v1.json")).jsonObject.getValue("free").jsonArray.map { it.jsonObject }
        for ((i, case) in cases.withIndex()) {
            val root = folder.newFolder("case-$i")
            case.getValue("taken").jsonArray.forEach { File(root, it.jsonPrimitive.content).mkdir() }
            val name = case.getValue("name").jsonPrimitive.content
            assertEquals(name, case.getValue("free").jsonPrimitive.content, FolderNames.free(root, name).name)
        }
    }
}
