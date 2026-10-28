package dev.mnema.app.ui

import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import dev.mnema.app.helper.SourceChoice
import dev.mnema.app.settings.SettingsStore
import dev.mnema.app.settings.sourceChoice
import dev.mnema.app.ui.record.Source
import dev.mnema.app.ui.record.toggled
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class SourceSettingsTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    private fun store() = SettingsStore(PreferenceDataStoreFactory.create(scope = scope) { File(folder.root, "settings.preferences_pb") })

    @After
    fun tearDown() = scope.cancel()

    @Test
    fun chipTogglesOnlyItsSourceAndTheChoiceIsRemembered() = runBlocking {
        val store = store()
        assertEquals(SourceChoice(mic = true, call = true), store.current().sourceChoice())

        store.update { it.toggled(Source.CALL) }
        assertEquals(SourceChoice(mic = true, call = false), store.current().sourceChoice())

        store.update { it.toggled(Source.MIC).toggled(Source.CALL) }
        assertEquals(SourceChoice(mic = false, call = true), store.current().sourceChoice())
        assertEquals(false, store.current().recordMic)
        assertEquals(true, store.current().recordCall)
    }
}
