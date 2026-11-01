package dev.mnema.app.ui

import dev.mnema.app.i18n.Ui
import dev.mnema.app.i18n.UiLang
import dev.mnema.app.sync.resource
import dev.mnema.app.ui.components.lengthLabel
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.double
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Test

/** Fixture shared with the desktop panel length-labels-v1: a recording's length reads the same on the phone and the Mac. */
class LengthLabelTest {
    @Test
    fun lengthReadsLikeTheDesktopPanel() {
        val cases = Json.parseToJsonElement(resource("length-labels-v1.json")).jsonObject.getValue("cases").jsonArray
        val before = Ui.lang
        try {
            for (case in cases.map { it.jsonObject }) {
                val seconds = case.getValue("seconds").jsonPrimitive.double
                for (lang in UiLang.entries) {
                    Ui.lang = lang
                    assertEquals("$seconds $lang", case.getValue(lang.tag).jsonPrimitive.content, lengthLabel(seconds))
                }
            }
        } finally {
            Ui.lang = before
        }
    }
}
