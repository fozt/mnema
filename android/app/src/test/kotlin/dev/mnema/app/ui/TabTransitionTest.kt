package dev.mnema.app.ui

import org.junit.Assert.assertEquals
import org.junit.Test

class TabTransitionTest {
    private val record = sequenceOf("record", null)
    private val recordingsList = sequenceOf("recordings/list", "recordings", null)
    private val recordingDetail = sequenceOf("recordings/recording?folder={folder}", "recordings", null)
    private val settings = sequenceOf("settings", null)
    private val onboarding = sequenceOf("onboarding", null)

    @Test
    fun tabOrderDecidesDirectionThroughNestedGraph() {
        // from, to, fallback direction -> forward or not
        val cases = listOf(
            Triple(record, recordingsList, false) to true,
            Triple(recordingsList, settings, false) to true,
            Triple(recordingDetail, settings, false) to true,
            Triple(recordingsList, record, true) to false,
            Triple(settings, recordingsList, true) to false,
            Triple(settings, recordingDetail, true) to false,
            // within one tab and outside tabs: by depth
            Triple(recordingsList, recordingDetail, true) to true,
            Triple(recordingDetail, recordingsList, false) to false,
            Triple(onboarding, record, true) to true,
            Triple(record, onboarding, false) to false,
        )
        for ((index, case) in cases.withIndex()) {
            val (from, to, otherwise) = case.first
            assertEquals("случай #$index", case.second, tabForward(from, to, otherwise = otherwise))
        }
    }

    // a screen, its tab
    private val screens = listOf(
        Triple("запись", record, Tab.RECORD),
        Triple("список", recordingsList, Tab.RECORDINGS),
        Triple("карточка", recordingDetail, Tab.RECORDINGS),
        Triple("настройки", settings, Tab.SETTINGS),
        Triple("мастер поверх записи", onboarding, null),
    )

    // another tab goes over Record as it was left (recording → Record → Recordings is the same recording)
    @Test
    fun tabsNeverPileUpSoBackFromAnyTabLeadsToRecord() {
        for ((name, screen, shown) in screens) {
            for (tab in Tab.entries.filter { it != shown }) {
                val move = tabMove(screen, tab)
                assertEquals("$name → ${tab.title}", TabMove(popTo = "record", keep = true, show = tab.takeIf { it != Tab.RECORD }), move)
            }
        }
    }

    @Test
    fun tappingTheShownTabReturnsToItsStart() {
        assertEquals(TabMove(popTo = "recordings/list", keep = false), tabMove(recordingDetail, Tab.RECORDINGS))
        assertEquals(TabMove(popTo = "recordings/list", keep = false), tabMove(recordingsList, Tab.RECORDINGS))
        assertEquals(TabMove(popTo = "record", keep = false), tabMove(record, Tab.RECORD))
        assertEquals(TabMove(popTo = "settings", keep = false), tabMove(settings, Tab.SETTINGS))
    }
}
