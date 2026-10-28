package dev.mnema.app.i18n

import dev.mnema.app.server.Connection
import dev.mnema.app.server.RunnerClient
import kotlinx.coroutines.runBlocking
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Test

class TrTest {
    private val before = Ui.lang

    @After
    fun restore() {
        Ui.lang = before
    }

    @Test
    fun languageTagsMapToRussianOrEnglish() {
        assertEquals(UiLang.RU, UiLang.of("ru"))
        assertEquals(UiLang.RU, UiLang.of("ru-RU"))
        assertEquals(UiLang.EN, UiLang.of("en"))
        assertEquals(UiLang.EN, UiLang.of("uk"))
        assertEquals(UiLang.EN, UiLang.of(""))
        assertEquals(UiLang.EN, UiLang.of(null))
    }

    @Test
    fun englishFillsPlaceholders() {
        Ui.lang = UiLang.EN
        assertEquals("Recording “Call” is ready", tr("Recording “{name}” is ready", "name" to "Call"))
        assertEquals("No placeholders {here}", tr("No placeholders {here}"))
    }

    @Test
    fun missingRussianEntryFallsBackToEnglish() {
        Ui.lang = UiLang.RU
        assertEquals("Not in the catalog: 3", tr("Not in the catalog: {n}", "n" to 3))
    }

    @Test
    fun englishPluralIsSingularOnlyForOne() {
        Ui.lang = UiLang.EN
        assertEquals("1 minute", plural(1, "{n} minute", "{n} minutes"))
        assertEquals("0 minutes", plural(0, "{n} minute", "{n} minutes"))
        assertEquals("21 minutes", plural(21, "{n} minute", "{n} minutes"))
        assertEquals("Delete audio of 2 recordings in Work?", plural(2, "Delete audio of {n} recording in {section}?", "Delete audio of {n} recordings in {section}?", "section" to "Work"))
    }

    @Test
    fun russianPluralFormsFollowTheLastTwoDigits() {
        val forms = mapOf(
            1 to 0, 21 to 0, 101 to 0, 1001 to 0, -1 to 0,
            2 to 1, 3 to 1, 4 to 1, 22 to 1, 34 to 1, 102 to 1, -3 to 1,
            0 to 2, 5 to 2, 11 to 2, 12 to 2, 13 to 2, 14 to 2, 15 to 2, 20 to 2, 25 to 2, 100 to 2, 111 to 2, 112 to 2, 114 to 2,
        )
        for ((n, form) in forms) assertEquals("n=$n", form, ruForm(n))
    }

    @Test
    fun valuesAreNotFilledAgain() {
        Ui.lang = UiLang.EN
        assertEquals("Folder “{n}” 2", tr("Folder “{name}” {n}", "name" to "{n}", "n" to 2))
        assertEquals("“{n}”: 3 files", plural(3, "“{name}”: {n} file", "“{name}”: {n} files", "name" to "{n}"))
    }

    @Test
    fun russianPluralWithoutCatalogEntryUsesEnglishForms() {
        Ui.lang = UiLang.RU
        assertEquals("5 files", plural(5, "{n} file", "{n} files"))
    }

    @Test
    fun russianTextComesFromTheCatalog() {
        val catalog = mapOf<String, Any>("Recording “{name}” is ready" to "Запись «{name}» готова")
        assertEquals("Запись «Звонок» готова", translate(catalog, UiLang.RU, "Recording “{name}” is ready", arrayOf("name" to "Звонок")))
        assertEquals("Recording “Call” is ready", translate(catalog, UiLang.EN, "Recording “{name}” is ready", arrayOf("name" to "Call")))
    }

    @Test
    fun russianPluralPicksOneOfThreeForms() {
        val catalog = mapOf<String, Any>("{n} minute" to listOf("{n} минута", "{n} минуты", "{n} минут"))
        fun ru(n: Int) = translatePlural(catalog, UiLang.RU, n, "{n} minute", "{n} minutes", emptyArray())
        assertEquals("1 минута", ru(1))
        assertEquals("3 минуты", ru(3))
        assertEquals("5 минут", ru(5))
        assertEquals("11 минут", ru(11))
        assertEquals("14 минут", ru(14))
        assertEquals("21 минута", ru(21))
        assertEquals("22 минуты", ru(22))
        assertEquals("2 minutes", translatePlural(catalog, UiLang.EN, 2, "{n} minute", "{n} minutes", emptyArray()))
    }

    @Test
    fun runnerRequestsCarryTheInterfaceLanguage() = runBlocking<Unit> {
        MockWebServer().use { server ->
            server.start()
            repeat(2) { server.enqueue(MockResponse.Builder().code(200).body("""{"ok":true,"llm":false}""").build()) }
            val http = RunnerClient.defaultHttp()
            val client = RunnerClient(Connection(server.url("/").toString().trimEnd('/'), null), http)
            Ui.lang = UiLang.EN
            client.capabilities()
            assertEquals("en", server.takeRequest().headers["Accept-Language"])
            Ui.lang = UiLang.RU
            client.capabilities()
            assertEquals("ru", server.takeRequest().headers["Accept-Language"])
            http.dispatcher.executorService.shutdown()
            http.connectionPool.evictAll()
        }
    }
}
