package dev.mnema.app.ui.settings

import dev.mnema.app.i18n.tr
import dev.mnema.app.server.Connection
import dev.mnema.app.server.ConnectionException
import dev.mnema.app.server.Health
import dev.mnema.app.server.NodeCheck
import dev.mnema.app.server.PASTE_INVITE
import dev.mnema.app.server.RunnerClient
import dev.mnema.app.server.RunnerException
import dev.mnema.app.server.formatConnection
import dev.mnema.app.server.message
import dev.mnema.app.server.parseConnection
import dev.mnema.app.settings.NodeEntry
import dev.mnema.app.settings.SettingsStore
import dev.mnema.app.settings.nodeOf

private val CHECKING: String get() = tr("Checking…")
private val SUMMARY: String get() = tr("Recognition and summary")
private val ASR_ONLY: String get() = tr("Recognition only")
private val DOWN: String get() = tr("Unavailable")
private val NEW_INVITE: String get() = tr("A new invitation is needed")
private val NO_MODEL: String get() = tr("Not set up")
private val BAD_TOKEN: String get() = tr("The server gave an unexpected answer")

/** Node caption in the list: what it can do or why it is unavailable; not checked yet shows "Checking…". */
fun nodeStatusLabel(check: NodeCheck?): String = when (check?.health) {
    null -> CHECKING
    Health.Ok -> if (check.llm) SUMMARY else ASR_ONLY
    Health.TokenRequired, Health.TokenRejected -> NEW_INVITE
    Health.Unreachable, Health.NotMnema -> DOWN
}

/** The canonical model name, as the node reported it. */
fun nodeModelSummary(model: String?): String = model?.takeIf { it.isNotBlank() } ?: NO_MODEL

/** Outcome of Add server. */
sealed interface JoinResult {
    data class Added(val address: String) : JoinResult

    data class Refused(val text: String) : JoinResult
}

/**
 * An invitation is exchanged for a device token at once and saved; an open server's address after
 * it answers; a string with a token is refused. The node name comes from its answer.
 */
suspend fun joinNode(
    text: String,
    device: String,
    connect: (Connection) -> RunnerClient,
    store: SettingsStore,
    joined: suspend (String) -> Unit,
): JoinResult {
    val parsed = try {
        parseConnection(text)
    } catch (e: ConnectionException) {
        return JoinResult.Refused(e.message.orEmpty())
    }
    if (parsed.token != null) return JoinResult.Refused(PASTE_INVITE)
    val invite = parsed.invite ?: return joinOpen(parsed.url, connect, store, joined)
    val token = try {
        connect(Connection(parsed.url, null)).claimInvite(invite, device)
    } catch (e: RunnerException) {
        return JoinResult.Refused(e.message.orEmpty())
    }
    val line = try {
        formatConnection(parsed.url, token)
    } catch (_: ConnectionException) {
        return JoinResult.Refused(BAD_TOKEN)
    }
    val entry = checkNotNull(nodeOf(line))
    saveNode(entry, store, joined)
    val check = connect(parseConnection(line)).capabilities()
    store.update { it.withNode(entry.copy(name = check.name ?: entry.name)) }
    return JoinResult.Added(entry.address)
}

private suspend fun joinOpen(url: String, connect: (Connection) -> RunnerClient, store: SettingsStore, joined: suspend (String) -> Unit): JoinResult {
    val check = connect(Connection(url, null)).capabilities()
    check.health.message()?.let { return JoinResult.Refused(it) }
    val entry = checkNotNull(nodeOf(url))
    saveNode(entry.copy(name = check.name ?: entry.name), store, joined)
    return JoinResult.Added(entry.address)
}

/** Another token than the saved one for this address: the last sync with it is no longer trusted. */
private suspend fun saveNode(entry: NodeEntry, store: SettingsStore, joined: suspend (String) -> Unit) {
    if (store.current().nodes.none { it.connection == entry.connection }) joined(entry.address)
    store.update { it.withNode(entry) }
}
