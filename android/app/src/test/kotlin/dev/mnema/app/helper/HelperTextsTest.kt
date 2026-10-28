package dev.mnema.app.helper

import dev.mnema.app.i18n.Ui
import dev.mnema.app.i18n.UiLang
import kotlinx.coroutines.test.runTest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import kotlin.coroutines.EmptyCoroutineContext

/** The helper writes English; the app shows it in the interface language. */
class HelperTextsTest {
    @get:Rule
    val folder = TemporaryFolder()

    @After fun russian() { Ui.lang = UiLang.RU }

    private val stages = listOf(
        Capture.NO_SYSTEM_AUDIO, Capture.MIC_UNAVAILABLE, Capture.NO_FOLDER_ACCESS, Capture.FILES_NOT_CREATED,
        Capture.SYSTEM_AUDIO_NOT_RECORDING, Capture.MIC_NOT_RECORDING, Capture.FILES_NOT_WRITTEN, Capture.SYSTEM_AUDIO_LOST,
        Capture.MIC_LOST, Failures.FILES_NOT_CLOSED, HelperMain.INTERNAL_FAILURE,
    )

    @Test fun everyHelperTextHasItsRussianText() {
        for (stage in stages) {
            assertNotEquals(stage, helperText(stage))
            assertNotEquals(stage, helperText(Heartbeat.failure(true, stage)))
        }
        assertEquals("Помощник не понял команду. Перезапустите его", helperText(HelperMain.UNKNOWN_COMMAND))
        assertEquals("Помощник остановлен: приложение обновлено", helperText(HelperMain.APP_UPDATED))
        assertEquals("Запись остановилась из-за ошибки: пропал микрофон", helperText(Heartbeat.failure(true, Capture.MIC_LOST)))
    }

    @Test fun englishInterfaceShowsTheHelperTextAsWritten() {
        Ui.lang = UiLang.EN
        val failure = Heartbeat.failure(false, Capture.MIC_UNAVAILABLE)
        assertEquals("Couldn't start recording: microphone unavailable", helperText(failure))
        for (stage in stages) assertEquals(stage, helperText(stage))
    }

    @Test fun unknownTextIsShownAsIs() {
        // an older helper wrote Russian, a newer one may add a stage this app does not know
        assertEquals("Не удалось начать запись: микрофон недоступен", helperText("Не удалось начать запись: микрофон недоступен"))
        assertEquals("Не удалось начать запись: a new stage", helperText(Heartbeat.failure(false, "a new stage")))
    }

    @Test fun statusErrorIsReadInTheInterfaceLanguage() = runTest {
        val dir = folder.root
        val client = HelperClient(dir, 0, { 0 }, backgroundScope, EmptyCoroutineContext)
        StatusWriter.write(dir, StatusWriter.Status().apply {
            state = "idle"
            nonce = "0123456789abcdef0123456789abcdef"
            error = Heartbeat.failure(false, Capture.NO_SYSTEM_AUDIO)
        })

        assertEquals("Не удалось начать запись: нет доступа к системному звуку", client.readStatus()!!.error)
        Ui.lang = UiLang.EN
        assertEquals("Couldn't start recording: no access to system audio", client.readStatus()!!.error)
    }
}
