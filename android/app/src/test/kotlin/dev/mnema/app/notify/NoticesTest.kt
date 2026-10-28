package dev.mnema.app.notify

import dev.mnema.app.launch.PairingState
import dev.mnema.app.launch.pairingOutcome
import dev.mnema.app.notify.RecordingNotice.Notice
import dev.mnema.app.notify.RecordingNotice.Notice.Kind
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class NoticesTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val folder = "2026-10-21_14-35"

    private fun result() = ResultNotice.of(tmp.newFolder(folder))

    /** Notifications that open the app. */
    private fun opening(): Map<String, NoticeSpec> {
        val result = result()
        return mapOf(
            "incoming call" to Notices.call(incoming = true),
            "call in progress" to Notices.call(incoming = false),
            "call ended" to Notices.callEnded(),
            "recording done" to Notices.processingDone(result, "Дейли"),
            "processing failed" to Notices.processingFailed(result, "Сервер не ответил"),
            "cancelled recording not deleted" to Notices.cancelNotDeleted(result, null),
            "update" to Notices.startHelper(),
            "pairing complete" to pairingOutcome(PairingState.Paired)!!,
            "pairing failed" to pairingOutcome(PairingState.Failed("Проверьте код и повторите"))!!,
        )
    }

    @Test
    fun tappedNoticeOpensTheAppAndLeavesTheShade() {
        for ((name, spec) in opening()) {
            assertNotEquals(name, NoticeTap.None, spec.tap)
            assertTrue(name, spec.autoCancel)
            assertFalse(name, spec.ongoing)
        }
    }

    @Test
    fun buttonOfANoticeThatOpensTheAppRemovesThatNotice() {
        for ((name, spec) in opening()) {
            assertTrue(name, spec.buttons.all { it.dismisses })
        }
        assertEquals("Record", listOf(NotificationActionReceiver.ACTION_START), Notices.call(incoming = true).buttons.map { it.action })
        assertEquals(2, Notices.callEnded().buttons.size)
        assertEquals("Retry", 1, pairingOutcome(PairingState.Failed("Нужен Wi-Fi"))!!.buttons.size)
    }

    @Test
    fun processedRecordingOpensItsCard() {
        val result = result()

        assertEquals(NoticeTap.Recording(result), Notices.processingDone(result, "Дейли").tap)
        assertEquals(NoticeTap.Recording(result), Notices.processingFailed(result, "Сервер не ответил").tap)
        assertEquals("no title — the folder name", folder, Notices.processingDone(result, " ").text)
    }

    @Test
    fun recordingNoticeStaysWhileTheRecordingGoes() {
        val running = Notice(Kind.RECORDING, startedMs = 1_000)
        val paused = Notice(Kind.RECORDING, startedMs = 1_000, paused = true, recordedMs = 61_000)

        for (spec in listOf(Notices.recording(running, folder), Notices.recording(paused, folder), Notices.cancelAsk(folder))) {
            assertTrue(spec.title, spec.ongoing)
            assertFalse(spec.title, spec.autoCancel)
            assertTrue(spec.title, spec.buttons.isNotEmpty() && spec.buttons.none { it.dismisses })
        }
    }

    @Test
    fun pairingInProgressHasNoOutcomeYet() {
        assertNull(pairingOutcome(PairingState.Pairing))
        assertNull(pairingOutcome(PairingState.Searching()))
    }
}
