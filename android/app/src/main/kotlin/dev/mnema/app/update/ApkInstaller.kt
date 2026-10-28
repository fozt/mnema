package dev.mnema.app.update

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.Uri
import android.provider.Settings
import java.io.File

/** Installs the APK over itself with a PackageInstaller session; the outcome arrives in InstallResultReceiver. */
class ApkInstaller(private val context: Context) {
    fun allowed(): Boolean = context.packageManager.canRequestPackageInstalls()

    /** started gets the new session number before commit: the outcome already carries it. */
    fun install(apk: File, started: (Int) -> Unit) {
        val installer = context.packageManager.packageInstaller
        // a previous session may be left unconfirmed: install one version at a time
        installer.mySessions.forEach { runCatching { installer.abandonSession(it.sessionId) } }
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(context.packageName)
            setSize(apk.length())
        }
        val id = installer.createSession(params)
        started(id)
        try {
            installer.openSession(id).use { session ->
                apk.inputStream().use { input ->
                    session.openWrite(apk.name, 0, apk.length()).use { output ->
                        input.copyTo(output)
                        session.fsync(output)
                    }
                }
                session.commit(resultSender(id))
            }
        } catch (e: Exception) {
            runCatching { installer.abandonSession(id) }
            throw e
        }
    }

    // the installer adds the outcome to the intent: the PendingIntent is mutable, the target is our own unexported receiver
    private fun resultSender(session: Int) = PendingIntent.getBroadcast(
        context,
        session,
        Intent(context, InstallResultReceiver::class.java).setPackage(context.packageName),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE,
    ).intentSender
}

/** The Android "Install unknown apps" screen for Mnema. */
fun installPermissionIntent(context: Context): Intent =
    Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.fromParts("package", context.packageName, null))
