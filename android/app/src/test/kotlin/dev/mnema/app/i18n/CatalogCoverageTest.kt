package dev.mnema.app.i18n

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Every English text in the app has its Russian entry, and every entry is still in use. */
class CatalogCoverageTest {
    private val sources: List<File> = File("src/main/kotlin").walkTopDown()
        .filter { it.isFile && it.extension == "kt" && "/dev/mnema/app/i18n/" !in it.invariantSeparatorsPath }
        .toList()

    private val scan: ScanResult by lazy {
        val all = sources.map { scanSource(it.readText(), it.path) }
        ScanResult(all.flatMap { it.texts }, all.flatMap { it.plurals }, all.flatMap { it.problems })
    }

    @Test
    fun sourcesAreFound() {
        assertTrue("run from the app module", sources.size > 50)
    }

    @Test
    fun everyTextIsOneLiteral() {
        assertEquals(emptyList<String>(), scan.problems)
    }

    @Test
    fun everyTextHasItsRussianEntry() {
        val missing = scan.texts.filter { RU[it.key] !is String }.map { "${it.where}: ${it.key}" } +
            scan.plurals.filter { forms(it.key) == null }.map { "${it.where}: ${it.key} (three plural forms)" }
        assertEquals(emptyList<String>(), missing.distinct())
    }

    @Test
    fun everyRussianEntryIsUsed() {
        val used = scan.texts.map { it.key }.toSet() + scan.plurals.map { it.key }
        assertEquals(emptyList<String>(), RU.keys.filter { it !in used }.sorted())
    }

    @Test
    fun russianTextsKeepThePlaceholders() {
        val wrong = mutableListOf<String>()
        for (found in scan.texts) {
            val ru = RU[found.key] as? String ?: continue
            if (placeholders(ru) != placeholders(found.key)) wrong += "${found.where}: ${found.key} → $ru"
        }
        for (found in scan.plurals) {
            val forms = forms(found.key) ?: continue
            // the count may be spelled out in a form ("одна запись"), every other value must stay
            val named = (placeholders(found.key) + placeholders(found.other!!)) - "n"
            for (form in forms) if (placeholders(form) - "n" != named) wrong += "${found.where}: ${found.key} → $form"
        }
        assertEquals(emptyList<String>(), wrong.distinct())
    }

    @Test
    fun areasAgreeOnSharedKeys() {
        val maps = listOf("RU_UI" to RU_UI, "RU_DATA" to RU_DATA, "RU_SYSTEM" to RU_SYSTEM)
        val conflicts = mutableListOf<String>()
        for ((i, a) in maps.withIndex()) for (b in maps.drop(i + 1)) {
            for (key in a.second.keys intersect b.second.keys) {
                if (a.second[key] != b.second[key]) conflicts += "${a.first} and ${b.first}: $key"
            }
        }
        assertEquals(emptyList<String>(), conflicts)
    }

    @Test
    fun catalogValuesAreTextsOrThreeForms() {
        val bad = RU.filterValues { value -> value !is String && (value as? List<*>)?.let { it.size == 3 && it.all { form -> form is String } } != true }
        assertEquals(emptyMap<String, Any>(), bad)
    }

    private fun forms(key: String): List<String>? =
        (RU[key] as? List<*>)?.takeIf { it.size == 3 && it.all { form -> form is String } }?.map { it as String }
}
