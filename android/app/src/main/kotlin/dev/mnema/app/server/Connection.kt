package dev.mnema.app.server

import dev.mnema.app.i18n.tr
import java.net.IDN
import java.text.Normalizer
import java.util.Locale

internal val PASTE_INVITE: String get() = tr("Paste the invitation from the server")
private val INVALID_TEXT: String get() = tr("Invalid invitation. Copy the whole invitation from the server")
private val SPLIT_TEXT: String get() = tr("Pass the address and the token separately.")

class ConnectionException(message: String) : IllegalArgumentException(message)

/** [token] or the one-time [invite] live in the fragment and never become part of an HTTP request URL. */
data class Connection(val url: String, val token: String?, val invite: String? = null) {
    override fun toString() = "Connection(url=$url)"
}

private class Parts(val scheme: String, val host: String, val port: Int?, val path: String, val secret: Pair<String, String>?) {
    val url: String get() = "$scheme://$host" + (port?.let { ":$it" } ?: "") + path
}

fun parseConnection(text: String): Connection {
    val parts = parseParts(text)
    val (field, value) = parts.secret ?: return Connection(parts.url, null)
    return if (field == INVITE) Connection(parts.url, null, value) else Connection(parts.url, value)
}

fun formatConnection(url: String, token: String?): String {
    val connection = parseConnection(url)
    if (connection.token != null || connection.invite != null) throw ConnectionException(SPLIT_TEXT)
    val line = connection.url + if (token.isNullOrEmpty()) "" else "#token=" + quotePlus(token)
    parseConnection(line)
    return line
}

private fun parseParts(text: String): Parts {
    val trimmed = text.trim(::isSpace)
    if (trimmed.isEmpty() || trimmed.any { isSpace(it) || it.code < 32 || it.code == 127 }) {
        throw ConnectionException(PASTE_INVITE)
    }
    return parseUrl(if ("://" in trimmed) trimmed else "http://$trimmed") ?: throw ConnectionException(INVALID_TEXT)
}

// Like Python str.isspace: U+0085 is the only Unicode space Kotlin does not count.
private fun isSpace(c: Char) = c.isWhitespace() || c == '\u0085'

private fun parseUrl(text: String): Parts? {
    val colon = text.indexOf(':')
    val scheme = text.substring(0, colon).lowercase(Locale.ROOT)
    val rest = text.substring(colon + 1)
    if ((scheme != "http" && scheme != "https") || !rest.startsWith("//")) return null
    val afterSlashes = rest.substring(2)
    val netlocEnd = afterSlashes.indexOfAny(charArrayOf('/', '?', '#')).let { if (it < 0) afterSlashes.length else it }
    val netloc = afterSlashes.substring(0, netlocEnd)
    val (beforeFragment, fragment) = afterSlashes.substring(netlocEnd).splitOnce('#')
    val (path, query) = beforeFragment.splitOnce('?')
    if (query.isNotEmpty() || !isNormalizationSafe(netloc)) return null
    val (host, port) = parseAuthority(netloc) ?: return null
    val secret = if (fragment.isEmpty()) null else parseSecret(fragment) ?: return null
    return Parts(scheme, host, port, path.trimEnd('/'), secret)
}

private fun String.splitOnce(delimiter: Char): Pair<String, String> {
    val index = indexOf(delimiter)
    return if (index < 0) this to "" else substring(0, index) to substring(index + 1)
}

// The same guard as urllib.parse: NFKC must not produce URL delimiters.
private fun isNormalizationSafe(netloc: String): Boolean {
    if (netloc.all { it.code < 128 }) return true
    val stripped = netloc.filterNot { it in "@:#?" }
    val normalized = Normalizer.normalize(stripped, Normalizer.Form.NFKC)
    return stripped == normalized || normalized.none { it in "/?#@:" }
}

private fun parseAuthority(netloc: String): Pair<String, Int?>? {
    val host: String
    val portText: String
    if ('[' in netloc || ']' in netloc) {
        val close = netloc.indexOf(']')
        if (!netloc.startsWith("[") || close < 0) return null
        val literal = netloc.substring(1, close).lowercase(Locale.ROOT)
        val after = netloc.substring(close + 1)
        if (after.isNotEmpty() && !after.startsWith(":")) return null
        if (parseIpv6(literal) == null) return null
        host = "[$literal]"
        portText = after.drop(1)
    } else {
        host = asciiHost(netloc.substringBefore(':')) ?: return null
        portText = netloc.substringAfter(':', "")
    }
    if (portText.isEmpty()) return host to null
    if (!portText.all { it in '0'..'9' }) return null
    val port = portText.toIntOrNull()?.takeIf { it in 1..65535 } ?: return null
    return host to port
}

private val HOST_CHARS = Regex("[a-z0-9._-]+")

// Labels as in the idna codec: none empty (except a trailing one) or longer than 63 characters.
private fun asciiHost(raw: String): String? {
    val lowered = raw.lowercase(Locale.ROOT)
    val ascii = if (lowered.all { it.code < 128 }) {
        val labels = lowered.split('.')
        if (labels.dropLast(1).any { it.isEmpty() || it.length > 63 } || labels.last().length > 63) return null
        lowered
    } else {
        try {
            IDN.toASCII(lowered)
        } catch (_: IllegalArgumentException) {
            return null
        }
    }
    return ascii.lowercase(Locale.ROOT).takeIf { HOST_CHARS.matches(it) }
}

private val STRAY_PERCENT = Regex("%(?![0-9a-fA-F]{2})")

private const val TOKEN = "token"
private const val INVITE = "invite"

// The fragment holds exactly one token=… or invite=… field; the decoded value is visible ASCII.
private fun parseSecret(fragment: String): Pair<String, String>? {
    if (STRAY_PERCENT.containsMatchIn(fragment) || '&' in fragment || '=' !in fragment) return null
    val (name, value) = fragment.splitOnce('=')
    val field = percentDecode(name)?.takeIf { it == TOKEN || it == INVITE } ?: return null
    val secret = percentDecode(value)?.takeIf { it.isNotEmpty() } ?: return null
    return field to secret
}

private fun percentDecode(text: String): String? {
    val out = StringBuilder()
    var i = 0
    while (i < text.length) {
        val c = text[i]
        val code = if (c == '%') text.substring(i + 1, i + 3).toInt(16).also { i += 2 } else c.code
        if (c == '+' || code !in 33..126) return null
        out.append(code.toChar())
        i++
    }
    return out.toString()
}

private const val HEX = "0123456789ABCDEF"

// urllib quote_plus: letters, digits and _.-~ are safe
private fun quotePlus(value: String): String = buildString {
    for (byte in value.toByteArray(Charsets.UTF_8)) {
        val code = byte.toInt() and 0xFF
        val c = code.toChar()
        when {
            c == ' ' -> append('+')
            code < 128 && (c.isLetterOrDigit() || c in "_.-~") -> append(c)
            else -> append('%').append(HEX[code shr 4]).append(HEX[code and 15])
        }
    }
}

private fun parseIpv4(text: String): IntArray? {
    val octets = text.split('.')
    if (octets.size != 4) return null
    return IntArray(4) { index ->
        val octet = octets[index]
        val plain = octet.length in 1..3 && octet.all { it in '0'..'9' } && (octet.length == 1 || octet[0] != '0')
        if (!plain) return null
        octet.toInt().takeIf { it <= 255 } ?: return null
    }
}

/** Eight IPv6 groups or null; a zone (`%eth0`) is not allowed. */
private fun parseIpv6(text: String): IntArray? {
    var rest = text
    if ('.' in rest) {
        val colon = rest.lastIndexOf(':')
        val v4 = (if (colon < 0) null else parseIpv4(rest.substring(colon + 1))) ?: return null
        val high = Integer.toHexString(v4[0] shl 8 or v4[1])
        val low = Integer.toHexString(v4[2] shl 8 or v4[3])
        rest = rest.substring(0, colon + 1) + high + ":" + low
    }
    val halves = rest.split("::")
    val groups = when (halves.size) {
        1 -> hextets(rest)?.takeIf { it.size == 8 }
        2 -> {
            val head = hextets(halves[0]) ?: return null
            val tail = hextets(halves[1]) ?: return null
            if (head.size + tail.size > 7) null else head + List(8 - head.size - tail.size) { 0 } + tail
        }
        else -> null
    }
    return groups?.toIntArray()
}

private fun hextets(text: String): List<Int>? {
    if (text.isEmpty()) return emptyList()
    return text.split(':').map { group ->
        val valid = group.length in 1..4 && group.all { it in '0'..'9' || it in 'a'..'f' || it in 'A'..'F' }
        if (!valid) return null
        group.toInt(16)
    }
}
