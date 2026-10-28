package dev.mnema.app.update

import android.content.ActivityNotFoundException
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.util.Log
import dev.mnema.app.MnemaApp
import dev.mnema.app.i18n.tr

private val OTHER_KEY: String get() = tr("The update is signed with a different key. It must be signed with the same key as the installed Mnema")
private val CONFLICT: String get() = tr("The system found a conflict while installing the update")
private val ABORTED: String get() = tr("Installation cancelled")
private val BLOCKED: String get() = tr("Installation is blocked on this phone")
private val INVALID: String get() = tr("The system didn't accept the update file")
private val INCOMPATIBLE: String get() = tr("The update isn't compatible with this phone")
private val STORAGE: String get() = tr("Not enough space to install")
private val TIMEOUT: String get() = tr("Installation didn't finish in time")

private const val LOG_TAG = "MnemaInstall"
private const val NO_SESSION = -1

// this is how PackageManager names an APK signed with the wrong key
private val SIGNATURE_MISMATCH = listOf("INSTALL_FAILED_UPDATE_INCOMPATIBLE", "signatures do not match")

sealed interface InstallOutcome {
    data object Confirm : InstallOutcome
    data object Success : InstallOutcome
    data class Failure(val text: String) : InstallOutcome
}

fun installOutcome(status: Int, message: String?): InstallOutcome {
    if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) return InstallOutcome.Confirm
    if (status == PackageInstaller.STATUS_SUCCESS) return InstallOutcome.Success
    val otherKey = SIGNATURE_MISMATCH.any { message?.contains(it, ignoreCase = true) == true }
    val text = when {
        otherKey -> OTHER_KEY
        status == PackageInstaller.STATUS_FAILURE_CONFLICT -> CONFLICT
        status == PackageInstaller.STATUS_FAILURE_ABORTED -> ABORTED
        status == PackageInstaller.STATUS_FAILURE_BLOCKED -> BLOCKED
        status == PackageInstaller.STATUS_FAILURE_INVALID -> INVALID
        status == PackageInstaller.STATUS_FAILURE_INCOMPATIBLE -> INCOMPATIBLE
        status == PackageInstaller.STATUS_FAILURE_STORAGE -> STORAGE
        status == PackageInstaller.STATUS_FAILURE_TIMEOUT -> TIMEOUT
        else -> INSTALL_FAILED
    }
    return InstallOutcome.Failure(text)
}

/** The stage after the install session outcome; null — the outcome of another or a cancelled session, the stage does not change. */
fun afterInstall(stage: UpdateStage, session: Int, outcome: InstallOutcome): UpdateStage? {
    val (manifest, apk) = when (stage) {
        is UpdateStage.Installing -> if (stage.session == session) stage.manifest to stage.apk else return null
        is UpdateStage.Confirm -> if (stage.session == session) stage.manifest to stage.apk else return null
        else -> return null
    }
    return when (outcome) {
        InstallOutcome.Success -> stage
        InstallOutcome.Confirm -> UpdateStage.Confirm(manifest, apk, session)
        is InstallOutcome.Failure -> UpdateStage.Failed(outcome.text, manifest, apk)
    }
}

/** Install session outcome: the system confirmation window or the reason for refusal. */
class InstallResultReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        val message = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)
        val session = intent.getIntExtra(PackageInstaller.EXTRA_SESSION_ID, NO_SESSION)
        val outcome = installOutcome(status, message)
        if (outcome is InstallOutcome.Failure) Log.w(LOG_TAG, "Installation failed: session $session, $status $message")
        if (!MnemaApp.graph(context).updates.installed(session, outcome)) return
        if (outcome == InstallOutcome.Confirm) confirm(context, intent)
    }

    private fun confirm(context: Context, intent: Intent) {
        val dialog = intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java) ?: return
        try {
            context.startActivity(dialog.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: ActivityNotFoundException) {
            Log.w(LOG_TAG, "Installation confirmation window did not open", e)
        }
    }
}
