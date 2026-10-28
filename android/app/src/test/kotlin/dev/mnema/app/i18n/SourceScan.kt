package dev.mnema.app.i18n

/** A `tr("…")` key, or the two English forms of a `plural(n, "…", "…")`, with its place in the sources. */
data class Found(val key: String, val other: String?, val where: String)

class ScanResult(val texts: List<Found>, val plurals: List<Found>, val problems: List<String>)

/**
 * Finds `tr` and `plural` calls in Kotlin source. Skips comments, strings and char literals, so
 * only real calls count. A call whose English text is not one plain literal (a variable, a `$`
 * template, a concatenation) is a problem: the catalog could not have its key.
 */
fun scanSource(src: String, file: String): ScanResult {
    val texts = mutableListOf<Found>()
    val plurals = mutableListOf<Found>()
    val problems = mutableListOf<String>()
    var i = 0
    while (i < src.length) {
        val c = src[i]
        when {
            src.startsWith("//", i) -> i = lineEnd(src, i)
            src.startsWith("/*", i) -> i = blockEnd(src, i)
            c == '"' -> i = readString(src, i).end
            c == '\'' -> i = charEnd(src, i)
            c.isJavaIdentifierStart() -> {
                val start = i
                while (i < src.length && src[i].isJavaIdentifierPart()) i++
                val word = src.substring(start, i)
                if ((word == "tr" || word == "plural") && isCall(src, start, i)) {
                    val where = "$file:${src.substring(0, start).count { it == '\n' } + 1}"
                    val open = skipBlank(src, i)
                    if (word == "tr") readTr(src, open + 1, where, texts, problems)
                    else readPlural(src, open + 1, where, plurals, problems)
                }
            }
            else -> i++
        }
    }
    return ScanResult(texts, plurals, problems)
}

private fun isCall(src: String, start: Int, end: Int): Boolean {
    val open = skipBlank(src, end)
    if (open >= src.length || src[open] != '(') return false
    var p = start - 1
    while (p >= 0 && src[p].isWhitespace()) p--
    // a method of something else, or a function reference `::tr`
    if (p >= 0 && (src[p] == '.' || (src[p] == ':' && p > 0 && src[p - 1] == ':'))) return false
    return !src.substring(0, start).trimEnd().endsWith("fun")
}

private fun readTr(src: String, from: Int, where: String, texts: MutableList<Found>, problems: MutableList<String>) {
    val lit = literalArg(src, from, where, "tr", problems) ?: return
    texts += Found(lit.value, null, where)
}

private fun readPlural(src: String, from: Int, where: String, plurals: MutableList<Found>, problems: MutableList<String>) {
    val comma = topLevelComma(src, from)
    if (comma < 0) {
        problems += "$where: plural needs a count and two literals"
        return
    }
    val one = literalArg(src, comma + 1, where, "plural", problems) ?: return
    val other = literalArg(src, one.next + 1, where, "plural", problems) ?: return
    plurals += Found(one.value, other.value, where)
}

private class Arg(val value: String, val next: Int)

/** Reads one argument that must be a plain string literal; [Arg.next] is the `,` or `)` after it. */
private fun literalArg(src: String, from: Int, where: String, call: String, problems: MutableList<String>): Arg? {
    val start = skipBlank(src, from)
    if (start >= src.length || src[start] != '"') {
        problems += "$where: $call takes the English text as a string literal"
        return null
    }
    val lit = readString(src, start)
    if (lit.template) {
        problems += "$where: $call text has a \$ template, use a {placeholder}"
        return null
    }
    val next = skipBlank(src, lit.end)
    if (next >= src.length || src[next] !in ",)") {
        problems += "$where: $call text must be one literal, not an expression"
        return null
    }
    return Arg(lit.value, next)
}

/** Index of the comma that ends the first argument, or -1 when the call has no second argument. */
private fun topLevelComma(src: String, from: Int): Int {
    var depth = 0
    var i = from
    while (i < src.length) {
        val c = src[i]
        when {
            src.startsWith("//", i) -> { i = lineEnd(src, i); continue }
            src.startsWith("/*", i) -> { i = blockEnd(src, i); continue }
            c == '"' -> { i = readString(src, i).end; continue }
            c == '\'' -> { i = charEnd(src, i); continue }
            c in "([{" -> depth++
            c in ")]}" -> if (depth == 0) return -1 else depth--
            c == ',' && depth == 0 -> return i
        }
        i++
    }
    return -1
}

private class Lit(val value: String, val end: Int, val template: Boolean)

private fun readString(src: String, start: Int): Lit {
    if (src.startsWith("\"\"\"", start)) {
        var end = src.indexOf("\"\"\"", start + 3)
        if (end < 0) end = src.length - 3
        while (end + 3 < src.length && src[end + 3] == '"') end++
        val value = src.substring(start + 3, end)
        return Lit(value, end + 3, Regex("""\$[\p{L}_{]""").containsMatchIn(value))
    }
    val out = StringBuilder()
    var template = false
    var i = start + 1
    while (i < src.length) {
        val c = src[i]
        when {
            c == '"' -> return Lit(out.toString(), i + 1, template)
            c == '\\' && i + 1 < src.length -> {
                val e = src[i + 1]
                if (e == 'u' && i + 5 < src.length) {
                    out.append(src.substring(i + 2, i + 6).toInt(16).toChar())
                    i += 6
                } else {
                    out.append(
                        when (e) {
                            'n' -> '\n'
                            't' -> '\t'
                            'r' -> '\r'
                            'b' -> '\b'
                            else -> e
                        },
                    )
                    i += 2
                }
                continue
            }
            c == '$' && i + 1 < src.length && src[i + 1] == '{' -> {
                template = true
                i = braceEnd(src, i + 1)
                continue
            }
            c == '$' && i + 1 < src.length && (src[i + 1].isLetter() || src[i + 1] == '_') -> template = true
        }
        out.append(c)
        i++
    }
    return Lit(out.toString(), src.length, template)
}

/** Index after the `}` that closes the `{` at [open], stepping over strings inside. */
private fun braceEnd(src: String, open: Int): Int {
    var depth = 0
    var i = open
    while (i < src.length) {
        when (src[i]) {
            '"' -> { i = readString(src, i).end; continue }
            '{' -> depth++
            '}' -> if (--depth == 0) return i + 1
        }
        i++
    }
    return src.length
}

private fun charEnd(src: String, start: Int): Int {
    if (start + 1 < src.length && src[start + 1] == '\\') {
        val close = src.indexOf('\'', start + 3)
        return if (close < 0) src.length else close + 1
    }
    return minOf(start + 3, src.length)
}

private fun lineEnd(src: String, from: Int): Int = src.indexOf('\n', from).let { if (it < 0) src.length else it }

/** Kotlin block comments nest. */
private fun blockEnd(src: String, from: Int): Int {
    var depth = 0
    var i = from
    while (i < src.length) {
        when {
            src.startsWith("/*", i) -> { depth++; i += 2 }
            src.startsWith("*/", i) -> { i += 2; if (--depth == 0) return i }
            else -> i++
        }
    }
    return src.length
}

private fun skipBlank(src: String, from: Int): Int {
    var i = from
    while (i < src.length) {
        when {
            src[i].isWhitespace() -> i++
            src.startsWith("//", i) -> i = lineEnd(src, i)
            src.startsWith("/*", i) -> i = blockEnd(src, i)
            else -> return i
        }
    }
    return i
}

private val PLACEHOLDER = Regex("""\{(\w+)\}""")

fun placeholders(text: String): Set<String> = PLACEHOLDER.findAll(text).map { it.groupValues[1] }.toSet()
