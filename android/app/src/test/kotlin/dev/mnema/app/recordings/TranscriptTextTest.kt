package dev.mnema.app.recordings

import dev.mnema.app.model.DialogLine
import dev.mnema.app.sync.resource
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.*
import org.junit.Test

class TranscriptTextTest {
    @Test fun timestampedParagraphsKeepOffsetsAndContinuationText() {
        assertEquals(listOf(DialogLine("mic", "А\nпродолжение", 12.0), DialogLine("system", "Б", 3723.0)),
            parseDialog("[0:12] mic: А\nпродолжение\n\n[1:02:03] system: Б"))
    }

    /** transcript.txt as the desktop with_speakers writes it (shared fixture): every paragraph stands alone, also without a track label. */
    @Test fun transcriptFileGivesTheSharedParagraphs() {
        val cases = Json.parseToJsonElement(resource("transcript-v1.json")).jsonObject.getValue("cases").jsonArray.map { it.jsonObject }
        for (case in cases) {
            val expected = case.getValue("dialog").jsonArray.map { it.jsonObject }.map { line ->
                DialogLine(line.getValue("speaker").jsonPrimitive.content, line.getValue("text").jsonPrimitive.content,
                    line.getValue("at").jsonPrimitive.doubleOrNull)
            }
            assertEquals(case.getValue("name").jsonPrimitive.content, expected, parseDialog(case.getValue("text").jsonPrimitive.content))
        }
    }

    @Test fun untimedAndMixedLinesAreRead() {
        assertEquals(listOf(DialogLine("mic", "А"), DialogLine("system", "Б"), DialogLine("mic", "В", 65.0)),
            parseDialog("заголовок\nmic: А\nsystem: Б\n[1:05] mic: В"))
        assertEquals(emptyList<DialogLine>(), parseDialog(""))
    }

    @Test fun dialogKeepsContinuationLinesAndSkipsWhatIsNotDialog() {
        val text = "заголовок без метки\n\nmic: Раз\nпродолжение\nsystem: Два\r\nsystem:\nmic: Три: четыре\n"
        assertEquals(listOf(DialogLine("mic", "Раз\nпродолжение"), DialogLine("system", "Два"), DialogLine("mic", "Три: четыре")),
            parseDialog(text))
    }

    @Test fun invalidMinutesMatchTheNodeTimestampContract() {
        val summary = parseSummaryFile("- [60:00] текст\n- [1:60:00] другой\n- [4:65] не время\n- [12:59:59] верный")
        assertEquals(listOf(null, null, null, 46799.0), summary.pointTimes)
        assertEquals(listOf("[60:00] текст", "[1:60:00] другой", "[4:65] не время", "верный"), summary.points)
    }

    @Test fun wrappedSummaryPointsKeepContinuationAndTimestamp() {
        val summary = parseSummaryFile("Обсудили выпуск.\n\n- Решили\n  не выпускать до 25 октября\n- [4:05] Даша взяла\nзадачу в работу\n")
        assertEquals("Обсудили выпуск.", summary.about)
        assertEquals(listOf("Решили\nне выпускать до 25 октября", "Даша взяла\nзадачу в работу"), summary.points)
        assertEquals(listOf(null, 245.0), summary.pointTimes)
    }

    @Test fun summaryFileGivesTheSharedGistAndPoints() {
        val cases = Json.parseToJsonElement(resource("summary-v1.json")).jsonObject.getValue("cases").jsonArray.map { it.jsonObject }
        for (case in cases) {
            val text = case.getValue("text").jsonPrimitive.content
            val summary = parseSummaryFile(text)
            assertEquals(text, case.getValue("about").jsonPrimitive.content, summary.about)
            assertEquals(text, case.getValue("points").jsonArray.map { it.jsonPrimitive.content }, summary.points)
            assertEquals(text, case.getValue("at").jsonArray.map { it.jsonPrimitive.doubleOrNull }, summary.pointTimes)
        }
    }
}
