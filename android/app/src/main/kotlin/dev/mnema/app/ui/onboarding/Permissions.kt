package dev.mnema.app.ui.onboarding

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationManager
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Environment
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import androidx.activity.compose.LocalActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.compose.LifecycleResumeEffect

private const val LOG_TAG = "MnemaAccess"

/** Access without which recording or its processing does not work properly. */
data class Access(val files: Boolean, val notifications: Boolean, val battery: Boolean)

fun readAccess(context: Context): Access = Access(
    files = Environment.isExternalStorageManager(),
    notifications = context.getSystemService(NotificationManager::class.java).areNotificationsEnabled(),
    battery = context.getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(context.packageName),
)

/** Access is reread on every return to the screen: it is changed in Android settings. */
@Composable
fun rememberAccess(): Access {
    val context = LocalContext.current
    var access by remember(context) { mutableStateOf(readAccess(context)) }
    LifecycleResumeEffect(context) {
        access = readAccess(context)
        onPauseOrDispose {}
    }
    return access
}

class AccessActions(val files: () -> Unit, val notifications: () -> Unit, val battery: () -> Unit)

@Composable
fun rememberAccessActions(): AccessActions {
    val context = LocalContext.current
    val activity by rememberUpdatedState(LocalActivity.current)
    // denied for good, or notifications are off in settings: the prompt will not show, so open settings
    val request = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        val asked = activity?.shouldShowRequestPermissionRationale(Manifest.permission.POST_NOTIFICATIONS) == true
        if (!readAccess(context).notifications && (granted || !asked)) open(context, notificationSettings(context))
    }
    return remember(context, request) {
        AccessActions(
            files = { open(context, filesAccess(context), Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION)) },
            notifications = { request.launch(Manifest.permission.POST_NOTIFICATIONS) },
            battery = {
                open(context, batteryExemption(context), Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            },
        )
    }
}

fun open(context: Context, intent: Intent, fallback: Intent? = null) {
    val intents = listOfNotNull(intent, fallback).map { it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) }
    for (candidate in intents) {
        try {
            context.startActivity(candidate)
            return
        } catch (e: ActivityNotFoundException) {
            Log.w(LOG_TAG, "Settings screen did not open: ${candidate.action}", e)
        }
    }
}

fun developerSettings(): Intent = Intent(Settings.ACTION_APPLICATION_DEVELOPMENT_SETTINGS)

private fun packageUri(context: Context): Uri = Uri.fromParts("package", context.packageName, null)

private fun filesAccess(context: Context) =
    Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, packageUri(context))

// battery optimization exemption: processing runs as a chain of background work
@SuppressLint("BatteryLife")
private fun batteryExemption(context: Context) =
    Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, packageUri(context))

private fun notificationSettings(context: Context) =
    Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)
