package dev.mnema.app.transcript

import dev.mnema.app.i18n.Ui
import dev.mnema.app.i18n.UiLang
import dev.mnema.app.model.Lang
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class RecordingFolderTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private fun folder(name: String): File = File(tmp.root, name).also { it.mkdirs() }

    @Test
    fun trackAndLanguageNamesFollowTheInterfaceLanguage() {
        val before = Ui.lang
        try {
            Ui.lang = UiLang.EN
            assertEquals(listOf("System audio", "Microphone"), RecordingFolder.TRACK_NAMES.values.toList())
            assertEquals(listOf("Automatic", "Russian", "English"), Lang.entries.map { it.title })
            Ui.lang = UiLang.RU
            assertEquals(listOf("Системный звук", "Микрофон"), RecordingFolder.TRACK_NAMES.values.toList())
            assertEquals(listOf("Автоматически", "Русский", "Английский"), Lang.entries.map { it.title })
        } finally { Ui.lang = before }
    }

    @Test
    fun sectionRecordIsAcceptedButNestedInsideARecordingIsNot() {
        val root = folder("Mnema")
        val nested = File(root, "a/b").also { it.mkdirs() }
        File(nested, "mic.wav").writeText("x")

        assertTrue(RecordingFolder.isCandidate(nested, root, null))
        File(nested.parentFile, "transcript.txt").writeText("mic: посторонний файл раздела")
        assertTrue(RecordingFolder.isCandidate(nested, root, null))
        File(nested.parentFile, ".mnema-meta.json").writeText("{}")
        assertFalse(RecordingFolder.isCandidate(nested, root, null))
        assertFalse(RecordingFolder.isCandidate(root, root, null))
    }
}
