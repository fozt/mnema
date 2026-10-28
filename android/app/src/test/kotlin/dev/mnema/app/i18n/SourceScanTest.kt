package dev.mnema.app.i18n

import org.junit.Assert.assertEquals
import org.junit.Test

class SourceScanTest {
    @Test
    fun findsCallsButNotCommentsStringsOrDefinitions() {
        val src = """
            // tr("in a line comment")
            /* tr("in a block /* nested */ comment") */
            fun tr(en: String) = en
            val quote = '"'
            val s = "tr(\"inside a string\")"
            val a = tr("Start recording")
            val b = x ?: tr(
                "Recording “{name}”",
                "name" to tr("Nested"),
            )
            val c = other.tr("Not ours")
            val d = list.map(::tr)
            val e = plural(items.count { it.ok }, "{n} file", "{n} files")
            val f = tr(${"\"\"\""}Raw "text"${"\"\"\""})
            val g = tr("Tab\t«q» \${'$'}")
        """.trimIndent()
        val result = scanSource(src, "A.kt")
        assertEquals(emptyList<String>(), result.problems)
        assertEquals(
            listOf("Start recording", "Recording “{name}”", "Nested", "Raw \"text\"", "Tab\t«q» \$"),
            result.texts.map { it.key },
        )
        assertEquals("A.kt:6", result.texts.first().where)
        assertEquals(listOf("{n} file" to "{n} files"), result.plurals.map { it.key to it.other })
    }

    @Test
    fun textsThatAreNotOneLiteralAreProblems() {
        val src = """
            val a = tr(name)
            val b = tr("Hello ${'$'}name")
            val c = tr("Hello " + name)
            val d = plural(n, one, "{n} files")
            val e = plural(n)
            val f = tr("Size ${'$'}{size.mb}")
        """.trimIndent()
        val result = scanSource(src, "B.kt")
        assertEquals(listOf("B.kt:1", "B.kt:2", "B.kt:3", "B.kt:4", "B.kt:5", "B.kt:6"), result.problems.map { it.substringBefore(": ") })
        assertEquals(emptyList<Found>(), result.texts + result.plurals)
    }

    @Test
    fun placeholdersAreNamedInBraces() {
        assertEquals(setOf("name", "n"), placeholders("“{name}”: {n} of {n}"))
        assertEquals(emptySet<String>(), placeholders("no { braces } here"))
    }
}
