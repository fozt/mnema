package dev.mnema.app.ui

import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import dev.mnema.app.settings.SettingsStore
import dev.mnema.app.settings.ThemeMode
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

class ThemeLogicTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val scopes = mutableListOf<CoroutineScope>()

    @After
    fun tearDown() = scopes.forEach { it.cancel() }

    @Test
    fun unknownStoredModeFallsBackToSystem() = runBlocking {
        val opened = openStore()
        opened.dataStore.edit { it[stringPreferencesKey("theme_mode")] = "SEPIA" }

        assertEquals(ThemeMode.SYSTEM, opened.store.current().themeMode)
    }

    private class Opened(val dataStore: DataStore<Preferences>, val store: SettingsStore)

    private fun openStore(): Opened {
        val scope = CoroutineScope(Dispatchers.IO + SupervisorJob()).also { scopes += it }
        val file = File(folder.root, "settings.preferences_pb")
        val dataStore = PreferenceDataStoreFactory.create(scope = scope) { file }
        return Opened(dataStore, SettingsStore(dataStore))
    }
}
