package dev.mnema.app.settings

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.core.IOException
import androidx.datastore.core.handlers.ReplaceFileCorruptionHandler
import androidx.datastore.preferences.core.MutablePreferences
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.emptyPreferences
import androidx.datastore.preferences.core.floatPreferencesKey
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import dev.mnema.app.helper.SourceChoice
import dev.mnema.app.i18n.tr
import dev.mnema.app.model.AUTO_ASR
import dev.mnema.app.model.Lang
import dev.mnema.app.server.ConnectionException
import dev.mnema.app.server.parseConnection
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.serialization.Serializable
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

enum class ThemeMode {
    SYSTEM,
    LIGHT,
    DARK;

    val title: String get() = when (this) {
        SYSTEM -> tr("System default")
        LIGHT -> tr("Light")
        DARK -> tr("Dark")
    }
}

enum class ColorSource {
    MNEMA,
    WALLPAPER;

    val title: String get() = when (this) {
        MNEMA -> "Mnema"
        WALLPAPER -> tr("From wallpaper")
    }
}

/** Language of the title and summary: `summary_lang` on desktop; AUTO — the language of the conversation. */
enum class SummaryLang(val code: String?) {
    AUTO(null),
    RU("ru"),
    EN("en");

    val title: String get() = when (this) {
        AUTO -> tr("Same as the conversation")
        RU -> tr("Russian")
        EN -> tr("English")
    }
}

/** Server: the connection string with this device's token and the name the server reported. */
@Serializable
data class NodeEntry(val connection: String, val name: String) {
    /** Node address without the token; a node is chosen by it and not added twice. */
    val address: String get() = nodeAddress(connection)

    override fun toString() = "NodeEntry(address=$address, name=$name)"
}

data class Settings(
    val nodes: List<NodeEntry> = emptyList(),
    // the selected server: processes recordings and syncs them
    val activeNode: String = "",
    val lang: Lang = Lang.AUTO,
    // id from the node list: a local engine or a cloud model "provider/model"
    val asr: String = AUTO_ASR,
    val onboardingDone: Boolean = false,
    val themeMode: ThemeMode = ThemeMode.SYSTEM,
    val colorSource: ColorSource = ColorSource.MNEMA,
    val summaryLang: SummaryLang = SummaryLang.AUTO,
    val recordMic: Boolean = true,
    val recordCall: Boolean = true,
    // section for the next recordings, chosen on "Recordings"; empty — no section
    val recordSection: String = "",
    val updateCheckedAt: Long = 0,
    val playbackSpeed: Float = 1f,
) {
    /** Connection string of the active node; no node selected — empty. */
    val connection: String get() = activeEntry()?.connection.orEmpty()

    fun activeEntry(): NodeEntry? = nodes.firstOrNull { activeNode.isNotEmpty() && it.address == activeNode }

    /** A node with the same address is replaced in place; the first node becomes active, the next ones wait to be chosen. */
    fun withNode(entry: NodeEntry): Settings {
        val known = nodes.any { it.address == entry.address }
        val list = if (known) nodes.map { if (it.address == entry.address) entry else it } else nodes + entry
        return copy(nodes = list, activeNode = if (activeEntry() == null) entry.address else activeNode)
    }

    /** A removed active node is not replaced by another: the next one is chosen by a tap. */
    fun withoutNode(address: String): Settings = copy(
        nodes = nodes.filterNot { it.address == address },
        activeNode = if (activeNode == address) "" else activeNode,
    )

    fun withActive(address: String): Settings = if (nodes.any { it.address == address }) copy(activeNode = address) else this

    fun withNodeName(address: String, name: String): Settings {
        val named = name.trim().ifEmpty { return this }
        return copy(nodes = nodes.map { if (it.address == address) it.copy(name = named) else it })
    }

}

/** Address from the connection string; an invalid string — empty. */
fun nodeAddress(connection: String): String = try {
    parseConnection(connection).url
} catch (_: ConnectionException) {
    ""
}

/** The node by connection string, named by its host until the node names itself; an invalid string — null. */
fun nodeOf(connection: String): NodeEntry? {
    val url = nodeAddress(connection).ifEmpty { return null }
    return NodeEntry(connection.trim(), url.toHttpUrlOrNull()?.host ?: url)
}

/** Track choice for the helper. */
fun Settings.sourceChoice(): SourceChoice = SourceChoice(mic = recordMic, call = recordCall)

private val Context.settingsDataStore: DataStore<Preferences> by preferencesDataStore(
    name = "settings",
    corruptionHandler = ReplaceFileCorruptionHandler { emptyPreferences() },
)

private val nodesJson = Json { ignoreUnknownKeys = true }
private val nodeList = ListSerializer(NodeEntry.serializer())

private object Keys {
    val nodes = stringPreferencesKey("nodes")
    val activeNode = stringPreferencesKey("active_node")
    val lang = stringPreferencesKey("lang")
    val asr = stringPreferencesKey("asr")
    val onboardingDone = booleanPreferencesKey("onboarding_done")
    val themeMode = stringPreferencesKey("theme_mode")
    val colorSource = stringPreferencesKey("color_source")
    val summaryLang = stringPreferencesKey("summary_lang")
    val recordMic = booleanPreferencesKey("record_mic")
    val recordCall = booleanPreferencesKey("record_call")
    val recordSection = stringPreferencesKey("record_section")
    val updateCheckedAt = longPreferencesKey("update_checked_at")
    val playbackSpeed = floatPreferencesKey("playback_speed")
}

class SettingsStore internal constructor(private val store: DataStore<Preferences>) {
    constructor(context: Context) : this(context.applicationContext.settingsDataStore)

    val settings: Flow<Settings> = store.data
        .catch { error -> if (error is IOException) emit(emptyPreferences()) else throw error }
        .map(Preferences::toSettings)

    suspend fun current(): Settings = settings.first()

    suspend fun update(transform: (Settings) -> Settings) {
        store.edit { prefs ->
            prefs.write(transform(prefs.toSettings()))
        }
    }
}

private fun Preferences.toSettings(): Settings {
    val saved = this[Keys.nodes]
    return Settings(
        nodes = saved?.let(::decodeNodes).orEmpty(),
        activeNode = this[Keys.activeNode].orEmpty(),
        lang = Lang.entries.firstOrNull { it.name == this[Keys.lang] } ?: Lang.AUTO,
        asr = this[Keys.asr]?.takeIf { it.isNotBlank() } ?: AUTO_ASR,
        onboardingDone = this[Keys.onboardingDone] ?: false,
        themeMode = ThemeMode.entries.firstOrNull { it.name == this[Keys.themeMode] } ?: ThemeMode.SYSTEM,
        colorSource = ColorSource.entries.firstOrNull { it.name == this[Keys.colorSource] } ?: ColorSource.MNEMA,
        summaryLang = SummaryLang.entries.firstOrNull { it.name == this[Keys.summaryLang] } ?: SummaryLang.AUTO,
        recordMic = this[Keys.recordMic] ?: true,
        recordCall = this[Keys.recordCall] ?: true,
        recordSection = this[Keys.recordSection].orEmpty(),
        updateCheckedAt = this[Keys.updateCheckedAt] ?: 0,
        playbackSpeed = this[Keys.playbackSpeed] ?: 1f,
    )
}

private fun decodeNodes(text: String): List<NodeEntry> = try {
    nodesJson.decodeFromString(nodeList, text)
} catch (_: IllegalArgumentException) {
    emptyList()
}

private fun MutablePreferences.write(settings: Settings) {
    this[Keys.nodes] = nodesJson.encodeToString(nodeList, settings.nodes)
    this[Keys.activeNode] = settings.activeNode
    this[Keys.lang] = settings.lang.name
    this[Keys.asr] = settings.asr
    this[Keys.onboardingDone] = settings.onboardingDone
    this[Keys.themeMode] = settings.themeMode.name
    this[Keys.colorSource] = settings.colorSource.name
    this[Keys.summaryLang] = settings.summaryLang.name
    this[Keys.recordMic] = settings.recordMic
    this[Keys.recordCall] = settings.recordCall
    this[Keys.recordSection] = settings.recordSection
    this[Keys.updateCheckedAt] = settings.updateCheckedAt
    this[Keys.playbackSpeed] = settings.playbackSpeed
}
