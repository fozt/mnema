package dev.mnema.app.helper

import dev.mnema.app.i18n.tr
import java.security.SecureRandom

object HelperProtocol {
    const val MAIN_CLASS = "dev.mnema.app.helper.HelperMain"
    @Suppress("SdCardPath")
    const val RECORDINGS_ROOT = "/sdcard/Documents/Mnema"
    const val CONTROL_DIR = "control"
    const val LOG = "/data/local/tmp/mnema-helper.log"
    const val ALIVE_MS = 6000L
    const val PING_WAIT_MS = 3000L
    const val ACTION_CALL_STARTED = "dev.mnema.app.CALL_STARTED"
    const val ACTION_RECORDING_STARTED = "dev.mnema.app.RECORDING_STARTED"
    const val ACTION_RECORDING_STOPPED = "dev.mnema.app.RECORDING_STOPPED"
    const val ACTION_CALL_ENDED = "dev.mnema.app.CALL_ENDED"
    // the call or the track sound changed: the notification checks status.json
    const val ACTION_STATE_CHANGED = "dev.mnema.app.STATE_CHANGED"
    const val REPORT = ".mnema-helper-report.json"
    // the last lines of the helper log at the end of a recording
    const val LOG_TAIL = ".mnema-helper-log.txt"
    // this many zeros in a row from the other side during a call — the call sound is not recorded
    const val SILENT_CALL_MS = 5000L

    private val NONCE = Regex("[0-9a-f]{32}")
    private val random = SecureRandom()

    fun newNonce(): String {
        val bytes = ByteArray(16).also(random::nextBytes)
        return bytes.joinToString("") { (it.toInt() and 0xff).toString(16).padStart(2, '0') }
    }

    /** Two shell commands: pkill and start. A sourceDir with "'" or a nonce other than [0-9a-f]{32} → IllegalArgumentException. */
    fun launchCommands(sourceDir: String, lastUpdateTime: Long, nonce: String): List<String> {
        require('\'' !in sourceDir) { tr("The APK path contains a single quote") }
        require(NONCE.matches(nonce)) { tr("The helper launch nonce must be 32 characters 0-9a-f") }
        // the brackets in the pattern keep pkill from killing its own shell
        val pattern = "[${MAIN_CLASS.first()}]${MAIN_CLASS.drop(1)}"
        return listOf(
            "pkill -f '$pattern'",
            "umask 077; CLASSPATH='$sourceDir' setsid nohup app_process / $MAIN_CLASS '$sourceDir' " +
                "$lastUpdateTime $nonce > $LOG 2>&1 < /dev/null &",
        )
    }
}
