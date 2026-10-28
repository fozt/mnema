package dev.mnema.app.launch

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.RemoteInput
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.os.IBinder
import android.os.SystemClock
import android.util.Log
import com.flyfishxu.kadb.Kadb
import dev.mnema.app.R
import dev.mnema.app.i18n.tr
import dev.mnema.app.notify.NoticeButton
import dev.mnema.app.notify.NoticeSpec
import dev.mnema.app.notify.Notifications
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.ConnectException

sealed interface PairingState {
    data object Idle : PairingState
    data class Searching(val windowClosed: Boolean = false) : PairingState
    data class WaitingCode(val port: Int) : PairingState
    data object Pairing : PairingState
    data object Paired : PairingState
    data class Failed(val message: String) : PairingState
}

/** Searching, waiting for the code, or pairing itself. */
val PairingState.inProgress: Boolean
    get() = this is PairingState.Searching || this is PairingState.WaitingCode || this is PairingState.Pairing

/** The key exists: the outcome of the previous attempt no longer applies, a pairing in progress stays. */
fun clearedWhenPaired(state: PairingState): PairingState = if (state.inProgress) state else PairingState.Idle

/** A Kadb.pair error: a closed port means the code window closed, wait for a new one; otherwise pairing failed. */
fun afterPairingError(error: Exception): PairingState =
    if (error is ConnectException) PairingState.Searching(windowClosed = true) else PairingState.Failed(PAIR_FAILED)

/** The latest port of the code window and a code that arrived before the port. */
class PairingPorts(private val now: () -> Long) {
    private var latest: Int? = null
    private var early: String? = null
    private var earlyAt = 0L

    /** A port was found; returns the code if it arrived before the port no more than 30 s ago. */
    fun found(port: Int): String? {
        latest = port
        val code = early?.takeIf { now() - earlyAt <= EARLY_CODE_MS }
        early = null
        return code
    }

    /** A code from the notification; returns the port for it, without a port keeps the code until the first one is found. */
    fun code(code: String): Int? {
        latest?.let { return it }
        early = code
        earlyAt = now()
        return null
    }

    /** The port refused the connection; returns a newer one if it is already found. */
    fun refused(port: Int): Int? {
        if (latest == port) latest = null
        return latest
    }
}

private const val EARLY_CODE_MS = 30_000L
private val PAIR_FAILED: String get() = tr("Check the code and try again")
private const val ACTION_START = "dev.mnema.app.launch.PAIR_START"
private val PAIRED_TITLE: String get() = tr("Pairing complete")
private val FAILED_TITLE: String get() = tr("Pairing failed")
private val RETRY: String get() = tr("Retry")

/** Pairing outcome: a tap opens the app and dismisses it, so does "Retry"; null — no outcome. */
internal fun pairingOutcome(result: PairingState): NoticeSpec? = when (result) {
    PairingState.Paired -> NoticeSpec(PAIRED_TITLE, autoCancel = true)
    is PairingState.Failed -> NoticeSpec(
        title = FAILED_TITLE,
        text = result.message,
        autoCancel = true,
        buttons = listOf(NoticeButton(RETRY, ACTION_START, dismisses = true)),
    )
    else -> null
}

class PairingService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var search: Job? = null
    private var pairing: Job? = null
    private val ports = PairingPorts(SystemClock::elapsedRealtime)
    private val notifications get() = getSystemService(NotificationManager::class.java)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val channel = NotificationChannel(CHANNEL, CHANNEL_NAME, NotificationManager.IMPORTANCE_HIGH).apply {
            setSound(null, null)
            setShowBadge(false)
        }
        notifications.createNotificationChannel(channel)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        intent?.let { Notifications.dismissFrom(this, it) }
        val current = mutableState.value.takeIf { it.inProgress } ?: PairingState.Searching()
        try {
            startForeground(ONGOING_ID, ongoing(current), ServiceInfo.FOREGROUND_SERVICE_TYPE_SHORT_SERVICE)
        } catch (e: RuntimeException) {
            Log.w(LOG_TAG, "startForeground failed", e)
            mutableState.value = PairingState.Failed(START_FAILED)
            stopSelf()
            return START_NOT_STICKY
        }
        when (intent?.action) {
            ACTION_REPLY -> onCode(RemoteInput.getResultsFromIntent(intent)?.getCharSequence(KEY_CODE)?.toString())
            ACTION_MANUAL -> pair(intent.getIntExtra(EXTRA_PORT, 0), intent.getStringExtra(EXTRA_CODE).orEmpty())
            ACTION_STOP -> finish(PairingState.Idle)
            else -> startSearch()
        }
        return START_NOT_STICKY
    }

    override fun onTimeout(startId: Int) {
        pairing?.cancel()
        finish(PairingState.Failed(TIMEOUT))
    }

    override fun onDestroy() {
        scope.cancel()
        if (mutableState.value.inProgress) mutableState.value = PairingState.Idle
        super.onDestroy()
    }

    private fun startSearch() {
        if (search?.isActive == true || pairing?.isActive == true) return show(mutableState.value)
        notifications.cancel(RESULT_ID)
        show(PairingState.Searching())
        search()
    }

    // discovery runs during pairing too: that is how the port of a reopened window becomes known
    private fun search() {
        if (search?.isActive == true) return
        search = scope.launch {
            if (!AdbPorts.wifiConnected(applicationContext)) return@launch finish(PairingState.Failed(NO_WIFI))
            AdbPorts.ports(applicationContext, AdbPorts.PAIRING).distinctUntilChanged().collect { onPort(it) }
            if (pairing?.isActive != true) finish(PairingState.Failed(START_FAILED))
        }
    }

    private fun onPort(port: Int) {
        val code = ports.found(port)
        when {
            pairing?.isActive == true -> Unit
            code != null -> pair(port, code)
            else -> show(PairingState.WaitingCode(port))
        }
    }

    private fun onCode(raw: String?) {
        if (pairing?.isActive == true) return show(PairingState.Pairing)
        if (raw.isNullOrBlank()) return startSearch()
        val port = ports.code(raw) ?: return startSearch()
        pair(port, raw)
    }

    private fun pair(port: Int, raw: String) {
        if (pairing?.isActive == true) return show(PairingState.Pairing)
        val code = raw.filterNot(Char::isWhitespace)
        if (port !in 1..65535 || code.isEmpty()) return finish(PairingState.Failed(BAD_INPUT))
        show(PairingState.Pairing)
        pairing = scope.launch {
            when (val result = pairWithAdbd(port, code)) {
                is PairingState.Searching -> windowClosed(port)
                else -> finish(result)
            }
        }
    }

    private fun windowClosed(port: Int) {
        val newer = ports.refused(port)
        show(newer?.let { PairingState.WaitingCode(it) } ?: PairingState.Searching(windowClosed = true))
        search()
    }

    private suspend fun pairWithAdbd(port: Int, code: String): PairingState = withContext(Dispatchers.IO) {
        val context = applicationContext
        val hadKey = AdbKey.exists(context)
        var paired = false
        try {
            AdbKey.configure(context)
            Kadb.pair(AdbPorts.HOST, port, code, AdbKey.NAME)
            paired = true
            PairingState.Paired
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Log.w(LOG_TAG, "Pairing with adbd failed", e)
            afterPairingError(e)
        } finally {
            // a failed attempt must not look like a pairing
            if (!paired && !hadKey) {
                runCatching { AdbKey.clear(context) }.onFailure { Log.w(LOG_TAG, "Key not removed", it) }
            }
        }
    }

    private fun show(state: PairingState) {
        mutableState.value = state
        notifications.notify(ONGOING_ID, ongoing(state))
    }

    private fun finish(result: PairingState) {
        search?.cancel()
        mutableState.value = result
        stopForeground(STOP_FOREGROUND_REMOVE)
        outcome(result)?.let { notifications.notify(RESULT_ID, it) }
        stopSelf()
    }

    private fun ongoing(state: PairingState): Notification {
        val builder = Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setCategory(Notification.CATEGORY_SERVICE)
            .setOngoing(true)
            .setOnlyAlertOnce(state !is PairingState.WaitingCode)
        when (state) {
            is PairingState.WaitingCode -> builder.setContentTitle(FOUND_TITLE).addAction(replyAction())
            PairingState.Pairing -> builder.setContentTitle(PAIRING_TITLE)
            is PairingState.Searching -> builder.setContentTitle(SEARCH_TITLE)
                .setContentText(if (state.windowClosed) WINDOW_CLOSED else SEARCH_TEXT)
            else -> builder.setContentTitle(SEARCH_TITLE).setContentText(SEARCH_TEXT)
        }
        return builder.addAction(action(ACTION_STOP, CANCEL, REQUEST_STOP)).build()
    }

    private fun outcome(result: PairingState): Notification? {
        val spec = pairingOutcome(result) ?: return null
        val builder = Notifications.noticeBuilder(this, CHANNEL, spec, Notifications.openApp(this))
        for (button in spec.buttons) {
            val intent = Intent(this, PairingService::class.java).setAction(button.action)
            if (button.dismisses) Notifications.dismissing(intent, RESULT_ID, null)
            val pending = PendingIntent.getForegroundService(this, REQUEST_RETRY, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            builder.addAction(Notifications.action(this, button.label, pending))
        }
        return builder.build()
    }

    private fun replyAction(): Notification.Action {
        val intent = Intent(this, PairingService::class.java).setAction(ACTION_REPLY)
        val reply = PendingIntent.getForegroundService(
            this,
            REQUEST_REPLY,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE,
        )
        val input = RemoteInput.Builder(KEY_CODE).setLabel(CODE_LABEL).build()
        return Notification.Action.Builder(icon(), ENTER_CODE, reply).addRemoteInput(input).build()
    }

    private fun action(name: String, title: String, request: Int): Notification.Action {
        val intent = Intent(this, PairingService::class.java).setAction(name)
        val pending = PendingIntent.getForegroundService(this, request, intent, PendingIntent.FLAG_IMMUTABLE)
        return Notification.Action.Builder(icon(), title, pending).build()
    }

    private fun icon(): Icon = Icon.createWithResource(this, R.drawable.ic_launcher_foreground)

    companion object {
        private val mutableState = MutableStateFlow<PairingState>(PairingState.Idle)
        val state: StateFlow<PairingState> = mutableState.asStateFlow()

        fun start(context: Context) = send(context, intent(context, ACTION_START))

        /** Call when the key exists: a previous failure is not shown next to "Paired". */
        fun clearOutcome() = mutableState.update(::clearedWhenPaired)

        fun submitManual(context: Context, port: Int, code: String) = send(
            context,
            intent(context, ACTION_MANUAL).putExtra(EXTRA_PORT, port).putExtra(EXTRA_CODE, code),
        )

        private fun intent(context: Context, action: String) =
            Intent(context, PairingService::class.java).setAction(action)

        private fun send(context: Context, intent: Intent) {
            try {
                context.startForegroundService(intent)
            } catch (e: IllegalStateException) {
                Log.w(LOG_TAG, "Pairing service did not start", e)
                mutableState.value = PairingState.Failed(START_FAILED)
            }
        }

        private const val CHANNEL = "pairing"
        private const val ONGOING_ID = 6001
        private const val RESULT_ID = 6002
        private const val REQUEST_REPLY = 1
        private const val REQUEST_STOP = 2
        private const val REQUEST_RETRY = 3
        private const val ACTION_REPLY = "dev.mnema.app.launch.PAIR_REPLY"
        private const val ACTION_MANUAL = "dev.mnema.app.launch.PAIR_MANUAL"
        private const val ACTION_STOP = "dev.mnema.app.launch.PAIR_STOP"
        private const val KEY_CODE = "code"
        private const val EXTRA_PORT = "port"
        private const val EXTRA_CODE = "code"

        private val CHANNEL_NAME: String get() = tr("Pairing")
        private val SEARCH_TITLE: String get() = tr("Looking for the pairing code window")
        private val SEARCH_TEXT: String get() = tr("“Wireless debugging”, then “Pair device with pairing code”")
        private val FOUND_TITLE: String get() = tr("Enter the pairing code")
        private val ENTER_CODE: String get() = tr("Enter code")
        private val CODE_LABEL: String get() = tr("Pairing code")
        private val PAIRING_TITLE: String get() = tr("Pairing")
        private val CANCEL: String get() = tr("Cancel")
        private val NO_WIFI: String get() = tr("Wi-Fi required")
        private val WINDOW_CLOSED: String get() = tr("The pairing code window closed. Open it again")
        private val TIMEOUT: String get() = tr("Pairing timed out")
        private val BAD_INPUT: String get() = tr("Check the port and the code")
        private val START_FAILED: String get() = tr("Pairing didn't start")
    }
}
