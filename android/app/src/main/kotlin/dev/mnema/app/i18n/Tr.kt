package dev.mnema.app.i18n

import kotlin.math.absoluteValue

/**
 * Interface text. [en] is the English source string, a literal at the call site: the Russian
 * catalog uses it as the key. Placeholders are `{name}`, filled from [args].
 */
fun tr(en: String, vararg args: Pair<String, Any?>): String = translate(RU, Ui.lang, en, args)

/**
 * Text that depends on the count [n], which also fills `{n}`. English: [one] for 1, [other]
 * otherwise. Russian: the catalog entry under [one] lists three forms for 1, 2–4 and 5+
 * (11–14 take the third).
 */
fun plural(n: Int, one: String, other: String, vararg args: Pair<String, Any?>): String =
    translatePlural(RU, Ui.lang, n, one, other, args)

internal fun translate(catalog: Map<String, Any>, lang: UiLang, en: String, args: Array<out Pair<String, Any?>>): String {
    val text = if (lang == UiLang.RU) catalog[en] as? String ?: en else en
    return fill(text, args)
}

internal fun translatePlural(
    catalog: Map<String, Any>,
    lang: UiLang,
    n: Int,
    one: String,
    other: String,
    args: Array<out Pair<String, Any?>>,
): String {
    val english = if (n == 1) one else other
    val text = when (lang) {
        UiLang.RU -> (catalog[one] as? List<*>)?.getOrNull(ruForm(n)) as? String ?: english
        UiLang.EN -> english
    }
    return fill(text, arrayOf("n" to n, *args))
}

/** Index of the Russian plural form: 0 — 1, 21, 101; 1 — 2–4, 22–24; 2 — the rest. */
internal fun ruForm(n: Int): Int {
    val m = n.toLong().absoluteValue
    return when {
        m % 10 == 1L && m % 100 != 11L -> 0
        m % 10 in 2L..4L && m % 100 !in 12L..14L -> 1
        else -> 2
    }
}

private val PLACEHOLDER = Regex("""\{(\w+)\}""")

/** One pass, so a value that itself contains `{name}` is left as is. Unknown placeholders stay. */
private fun fill(text: String, args: Array<out Pair<String, Any?>>): String {
    if (args.isEmpty()) return text
    val values = args.toMap()
    return PLACEHOLDER.replace(text) { match ->
        val name = match.groupValues[1]
        if (name in values) values[name].toString() else match.value
    }
}
