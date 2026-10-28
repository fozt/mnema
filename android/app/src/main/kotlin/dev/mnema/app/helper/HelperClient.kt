package dev.mnema.app.helper

import android.content.Context
import android.content.pm.PackageManager
import android.os.SystemClock
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.Json
import java.io.File
import java.io.IOException
import java.util.UUID
import kotlin.coroutines.CoroutineContext
import dev.mnema.app.i18n.tr

/** Helper heartbeat and commands through files in the control folder. */
class HelperClient(
    val controlDir: File,
    private val apkLastUpdateTime: Long,
    private val uptime: () -> Long,
    private val scope: CoroutineScope,
    private val ioContext: CoroutineContext = Dispatchers.IO,
) {
    private val statusFile = File(controlDir, StatusWriter.FILE)
    private val commandFile = File(controlDir, Commands.FILE)
    private var current: HelperState = HelperState.Unknown
    private var verdict: Verdict? = null

    val state: StateFlow<HelperState> = flow {
        while (true) {
            current = poll(current)
            emit(current)
            delay(POLL_MS)
        }
    }.stateIn(scope, SharingStarted.WhileSubscribed(), HelperState.Unknown)

    /** null: no file, truncated JSON or another protocol. */
    fun readStatus(): HelperStatus? = decode()?.takeIf { it.v == StatusWriter.VERSION }

    /** The stale heartbeat check runs before the file lock; an answer already known is reused. */
    internal suspend fun prepareMutation() { poll(current) }

    /** An unknown state does not allow changing the folder the helper may be writing to. */
    internal fun recordingFolderForMutation(): String? {
        val status = readStatus()
        if (status == null) {
            if (statusFile.exists() || !controlDir.canRead()) throw IOException(tr("Couldn't check the recording state"))
            return null
        }
        if (uptime() - status.heartbeatUptimeMs !in 0 until HelperProtocol.ALIVE_MS) {
            val known = verdict?.takeIf { it.heartbeat == status.heartbeatUptimeMs }
                ?: throw IOException(tr("Couldn't check the recording state"))
            if (!known.alive) return null
        }
        if (status.recording && status.folder.isEmpty()) throw IOException(tr("Couldn't check the recording state"))
        return status.takeIf { it.recording }?.folder
    }

    /** Command uuid; IOException goes out. */
    fun send(command: HelperCommand): String {
        val uuid = UUID.randomUUID().toString()
        val line = "${command.wire} $uuid"
        StatusWriter.writeAtomic(commandFile, line.toByteArray(Charsets.UTF_8), true)
        return uuid
    }

    /** The [choice] first, then the command; false — the choice was not applied (the command was not sent) or the command left no trace. */
    suspend fun command(command: HelperCommand, choice: SourceChoice? = null, timeoutMs: Long = HelperProtocol.PING_WAIT_MS): Boolean =
        COMMANDS.withLock {
            if (choice != null && !applyLocked(timeoutMs) { choice }) return@withLock false
            exchange(command, timeoutMs)
        }

    /** Commands up to the choice one by one, each after the trace of the previous; true — the helper records the choice. */
    suspend fun applySources(timeoutMs: Long = HelperProtocol.PING_WAIT_MS, choice: () -> SourceChoice?): Boolean =
        COMMANDS.withLock { applyLocked(timeoutMs, choice) }

    suspend fun ping(timeoutMs: Long = HelperProtocol.PING_WAIT_MS): Boolean = COMMANDS.withLock {
        val uuid = withContext(ioContext) {
            try {
                send(HelperCommand.PING)
            } catch (_: Exception) {
                null
            }
        } ?: return@withLock false
        awaitPong(uuid, timeoutMs)
    }

    suspend fun awaitNonce(nonce: String, timeoutMs: Long = 5000): Boolean =
        await(timeoutMs) { readStatus()?.nonce == nonce }

    private suspend fun poll(previous: HelperState): HelperState {
        val (status, present) = withContext(ioContext) { if (statusFile.exists()) decode() to true else null to false }
        if (!present) {
            verdict = null
            return HelperState.NotRunning
        }
        if (status == null) return previous // truncated JSON — the previous value
        if (status.v != StatusWriter.VERSION) {
            verdict = null
            return HelperState.Running(status, outdated = true)
        }
        val age = uptime() - status.heartbeatUptimeMs
        if (age in 0 until HelperProtocol.ALIVE_MS) {
            verdict = null
            return running(status)
        }
        // stale heartbeat: one ping per heartbeat, the answer is remembered
        val known = verdict
        if (known != null && known.heartbeat == status.heartbeatUptimeMs) {
            return if (known.alive) running(status) else HelperState.NotRunning
        }
        val alive = ping()
        verdict = Verdict(status.heartbeatUptimeMs, alive)
        return if (alive) running(withContext(ioContext) { readStatus() } ?: status) else HelperState.NotRunning
    }

    private fun decode(): HelperStatus? {
        val text = try {
            if (!statusFile.exists()) return null
            statusFile.readText()
        } catch (_: Exception) {
            return null
        }
        return try {
            // the helper writes its error in English: every reader gets it in the interface language
            JSON.decodeFromString<HelperStatus>(text).let { it.copy(error = helperText(it.error)) }
        } catch (_: Exception) {
            null
        }
    }

    private fun running(status: HelperStatus) =
        HelperState.Running(status, outdated = status.apkUpdated != apkLastUpdateTime)

    private suspend fun applyLocked(timeoutMs: Long, choice: () -> SourceChoice?): Boolean {
        while (true) {
            val wanted = choice() ?: return true
            val status = withContext(ioContext) { readStatus() } ?: return false
            val next = nextSourceCommand(status, wanted) ?: return true
            val applied = try {
                exchange(next, timeoutMs)
            } catch (_: IOException) {
                false
            }
            if (!applied) return false
        }
    }

    private suspend fun exchange(command: HelperCommand, timeoutMs: Long): Boolean {
        val uuid = withContext(ioContext) { send(command) }
        return await(timeoutMs) { readStatus()?.shows(command, uuid) == true }
    }

    private suspend fun awaitPong(uuid: String, timeoutMs: Long): Boolean =
        await(timeoutMs) { readStatus()?.pong == uuid }

    private suspend fun await(timeoutMs: Long, done: suspend () -> Boolean): Boolean =
        withTimeoutOrNull(timeoutMs) {
            while (!withContext(ioContext) { done() }) delay(WAIT_STEP_MS)
            true
        } ?: false

    private class Verdict(val heartbeat: Long, val alive: Boolean)

    companion object {
        private const val POLL_MS = 500L
        private const val WAIT_STEP_MS = 100L
        private val JSON = Json { ignoreUnknownKeys = true }
        // one command.txt: commands from different clients of the process don't overwrite each other
        private val COMMANDS = Mutex()

        private val NO_CONTROL_DIR: String get() = tr("No access to phone storage")

        fun forContext(context: Context, scope: CoroutineScope): HelperClient {
            val control = checkNotNull(context.getExternalFilesDir(HelperProtocol.CONTROL_DIR)) { NO_CONTROL_DIR }
            val info = context.packageManager.getPackageInfo(context.packageName, PackageManager.PackageInfoFlags.of(0))
            return HelperClient(control, info.lastUpdateTime, SystemClock::uptimeMillis, scope)
        }
    }
}

/** The command the helper still needs to reach [choice]; null — it already records that way. */
fun nextSourceCommand(status: HelperStatus, choice: SourceChoice): HelperCommand? = when {
    status.micOn != choice.mic -> if (choice.mic) HelperCommand.MIC_ON else HelperCommand.MIC_OFF
    status.callOn != choice.call -> if (choice.call) HelperCommand.CALL_ON else HelperCommand.CALL_OFF
    else -> null
}

private fun HelperStatus.shows(command: HelperCommand, uuid: String): Boolean = when (command) {
    HelperCommand.START -> recording
    HelperCommand.STOP -> !recording
    HelperCommand.PING -> pong == uuid
    HelperCommand.MIC_ON -> micOn == true
    HelperCommand.MIC_OFF -> micOn == false
    HelperCommand.CALL_ON -> callOn == true
    HelperCommand.CALL_OFF -> callOn == false
    HelperCommand.PAUSE -> paused == true
    HelperCommand.RESUME -> paused == false
}
