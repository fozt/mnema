package dev.mnema.app.server

import dev.mnema.app.i18n.tr

/** Which side to update when the app and the server differ in MAJOR.MINOR, like `connection.py::VersionGap`. */
enum class VersionGap { APP, SERVER }

private val RELEASE = Regex("""(\d+)\.(\d+)(?:\.\d+)?""")

/** The `version` of `GET /api/capabilities` against this app; a patch difference is fine, a server without a readable version is not judged. */
fun versionGap(server: String?, app: String): VersionGap? {
    val theirs = server?.let(RELEASE::matchEntire)?.release() ?: return null
    val ours = RELEASE.matchEntire(app)?.release() ?: return null
    val order = compareValuesBy(theirs, ours, { it.first }, { it.second })
    return when {
        order > 0 -> VersionGap.APP
        order < 0 -> VersionGap.SERVER
        else -> null
    }
}

private fun MatchResult.release(): Pair<Int, Int>? {
    val (major, minor) = destructured
    return (major.toIntOrNull() ?: return null) to (minor.toIntOrNull() ?: return null)
}

/** One message instead of sync errors when the server runs another Mnema version; null — the versions fit or are unknown. */
fun versionNotice(server: String?, app: String): String? = when (versionGap(server, app)) {
    VersionGap.APP -> tr("The server runs Mnema {server}. Update the app", "server" to server.orEmpty())
    VersionGap.SERVER -> tr("The server runs Mnema {server}, older than this app. Update the server", "server" to server.orEmpty())
    null -> null
}
