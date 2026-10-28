package dev.mnema.app.launch

import android.content.Context
import android.content.pm.PackageManager
import android.util.Log
import com.flyfishxu.kadb.Kadb
import com.flyfishxu.kadb.cert.KadbCert
import com.flyfishxu.kadb.cert.OkioFilePrivateKeyStore
import com.flyfishxu.kadb.exception.AdbPairAuthException
import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.i18n.tr
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okio.Path.Companion.toOkioPath
import java.io.File

class KadbLauncher(private val context: Context) : HelperLauncher {
    override val paired: Boolean get() = AdbKey.exists(context)

    override suspend fun launch(awaitNonce: suspend (String) -> Boolean): LaunchResult = withContext(Dispatchers.IO) {
        if (!paired) return@withContext LaunchResult.NotPaired
        if (!AdbPorts.wifiConnected(context)) return@withContext LaunchResult.NoWifi
        val port = AdbPorts.find(context, AdbPorts.CONNECT, PORT_WAIT_MS) ?: run {
            Log.w(LOG_TAG, "${AdbPorts.CONNECT}: port not found in $PORT_WAIT_MS ms")
            return@withContext LaunchResult.Failed(NO_DEBUGGING)
        }
        val nonce = HelperProtocol.newNonce()
        val commands = try {
            HelperProtocol.launchCommands(context.applicationInfo.sourceDir, lastUpdateTime(), nonce)
        } catch (e: IllegalArgumentException) {
            return@withContext LaunchResult.Failed(e.message ?: START_FAILED)
        }
        AdbKey.configure(context)
        Kadb.create(AdbPorts.HOST, port, CONNECT_TIMEOUT_MS, SOCKET_TIMEOUT_MS).use { adb ->
            runCommands(adb, commands) ?: if (awaitNonce(nonce)) LaunchResult.Started else LaunchResult.Failed(NO_ANSWER)
        }
    }

    override fun forget() = AdbKey.clear(context)

    private fun lastUpdateTime(): Long =
        context.packageManager.getPackageInfo(context.packageName, PackageManager.PackageInfoFlags.of(0)).lastUpdateTime

    private fun runCommands(adb: Kadb, commands: List<String>): LaunchResult? = try {
        val last = commands.map { adb.shell(it) }.last()
        if (last.exitCode == 0) {
            null
        } else {
            Log.w(LOG_TAG, "Helper start: exit code ${last.exitCode}, output: ${last.allOutput.take(OUTPUT_LIMIT)}")
            LaunchResult.Failed(START_FAILED)
        }
    } catch (e: AdbPairAuthException) {
        Log.w(LOG_TAG, "adbd did not accept the key", e)
        LaunchResult.NotPaired
    } catch (e: Exception) {
        Log.w(LOG_TAG, "Connecting to adbd failed", e)
        LaunchResult.Failed(CONNECT_FAILED)
    }

    private companion object {
        const val PORT_WAIT_MS = 5000L
        const val CONNECT_TIMEOUT_MS = 5000
        const val SOCKET_TIMEOUT_MS = 10000
        const val OUTPUT_LIMIT = 500
        val NO_DEBUGGING: String get() = tr("Wireless debugging not found")
        val CONNECT_FAILED: String get() = tr("Couldn't connect to wireless debugging")
        val START_FAILED: String get() = tr("Couldn't start the helper")
        val NO_ANSWER: String get() = tr("The helper didn't answer")
    }
}

internal object AdbKey {
    const val NAME = "Mnema"
    private var configured = false

    fun file(context: Context): File = File(File(context.noBackupFilesDir, "adb"), "adbkey")

    fun exists(context: Context): Boolean = file(context).let { it.isFile && it.length() > 0 }

    @Synchronized
    fun configure(context: Context) {
        if (configured) return
        KadbCert.configure(OkioFilePrivateKeyStore(file(context).toOkioPath()))
        configured = true
    }

    fun clear(context: Context) {
        configure(context)
        KadbCert.clear()
    }
}
