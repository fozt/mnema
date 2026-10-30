package dev.mnema.app.update

import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import dev.mnema.app.i18n.tr
import dev.mnema.app.settings.SettingsStore
import java.io.File
import java.io.IOException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request

const val MANIFEST_URL = "https://github.com/fozt/mnema/releases/latest/download/mnema-android.json"

internal val NO_NETWORK: String get() = tr("No connection to the update server")
internal val NOT_PUBLISHED: String get() = tr("Updates are unavailable right now")
internal val INSTALL_FAILED: String get() = tr("Couldn't install the update")

private const val CHECK_EVERY_MS = 12 * 60 * 60 * 1000L
private const val MAX_MANIFEST_BYTES = 64L * 1024
private const val LOG_TAG = "MnemaUpdate"

/** Time to check: never checked, 12 hours have passed, or the clock was set back. */
fun due(checkedAt: Long, now: Long): Boolean = checkedAt <= 0 || now - checkedAt !in 0 until CHECK_EVERY_MS

/** The app came to the foreground: check when it is due and nothing is being downloaded or installed. */
fun checksOnForeground(stage: UpdateStage, checkedAt: Long, now: Long): Boolean = due(checkedAt, now) && when (stage) {
    UpdateStage.Idle, UpdateStage.UpToDate, is UpdateStage.Unsuitable, is UpdateStage.Available -> true
    is UpdateStage.Failed -> stage.manifest == null
    else -> false
}

/** A newer version waits to be installed: the Settings tab carries a dot. */
fun updatePending(stage: UpdateStage): Boolean = when (stage) {
    is UpdateStage.Available, is UpdateStage.Downloading, is UpdateStage.NeedsPermission,
    is UpdateStage.Ready, is UpdateStage.Installing, is UpdateStage.Confirm,
    -> true
    is UpdateStage.Failed -> stage.manifest != null
    else -> false
}

/** The manifest of the latest release, by an anonymous GET. */
suspend fun fetchManifest(http: OkHttpClient, url: String = MANIFEST_URL): UpdateManifest = withContext(Dispatchers.IO) {
    val text = try {
        http.newCall(Request.Builder().url(url).build()).execute().use { response ->
            // GitHub answers 404 when there is no release
            if (response.code in setOf(401, 403, 404)) throw UpdateException(NOT_PUBLISHED)
            if (!response.isSuccessful) throw UpdateException(BAD_MANIFEST)
            response.peekBody(MAX_MANIFEST_BYTES).string()
        }
    } catch (_: IOException) {
        throw UpdateException(NO_NETWORK)
    }
    parseManifest(text)
}

/** Update stage; what this process knows. */
sealed interface UpdateStage {
    data object Idle : UpdateStage
    data object Checking : UpdateStage
    data object UpToDate : UpdateStage
    data class Unsuitable(val manifest: UpdateManifest) : UpdateStage
    data class Available(val manifest: UpdateManifest) : UpdateStage
    data class Downloading(val manifest: UpdateManifest, val fraction: Float) : UpdateStage
    data class NeedsPermission(val manifest: UpdateManifest, val apk: File) : UpdateStage
    data class Ready(val manifest: UpdateManifest, val apk: File) : UpdateStage

    /** session — the PackageInstaller session number; null until the system gives it. */
    data class Installing(val manifest: UpdateManifest, val apk: File, val session: Int? = null) : UpdateStage
    data class Confirm(val manifest: UpdateManifest, val apk: File, val session: Int) : UpdateStage

    /** What failed; the step that has everything is retried: install, download or check. */
    data class Failed(val text: String, val manifest: UpdateManifest? = null, val apk: File? = null) : UpdateStage
}

/** Checks, downloads and installs releases from the registry; one instance per process. */
class UpdateChecker(
    private val context: Context,
    private val http: OkHttpClient,
    private val settings: SettingsStore,
    private val scope: CoroutineScope,
    // the folder the helper is writing to now; null — not recording
    private val recordingFolder: suspend () -> String?,
) {
    private val downloader = ApkDownloader(http, File(context.cacheDir, "updates"))
    private val installer = ApkInstaller(context)
    private val lastManifest = File(context.filesDir, "update-manifest.json")
    private val state = MutableStateFlow<UpdateStage>(UpdateStage.Idle)
    private var work: Job? = null

    val stage: StateFlow<UpdateStage> = state.asStateFlow()

    val versionName: String by lazy { packageInfo()?.versionName.orEmpty() }

    /** The app came to the foreground: check every 12 hours, without notifications; at start, the outcome of the previous check. */
    fun checkIfDue() = launchOnce {
        val now = state.value
        if (checksOnForeground(now, settings.current().updateCheckedAt, System.currentTimeMillis())) {
            runCheck()
        } else if (now == UpdateStage.Idle) {
            val saved = withContext(Dispatchers.IO) { loadManifest(lastManifest) } ?: return@launchOnce
            state.value = decide(installedCode(), saved, Build.VERSION.SDK_INT)
        }
    }

    fun check() = launchOnce { runCheck() }

    fun update() {
        val manifest = when (val now = state.value) {
            is UpdateStage.Available -> now.manifest
            is UpdateStage.Failed -> now.manifest ?: return
            else -> return
        }
        launchOnce {
            state.value = UpdateStage.Downloading(manifest, 0f)
            val apk = attempt({ UpdateStage.Failed(it, manifest) }, DOWNLOAD_FAILED) {
                downloader.download(manifest) { state.value = UpdateStage.Downloading(manifest, it) }
            } ?: return@launchOnce
            runInstall(manifest, apk)
        }
    }

    fun install() {
        val (manifest, apk) = when (val now = state.value) {
            is UpdateStage.Ready -> now.manifest to now.apk
            is UpdateStage.Confirm -> now.manifest to now.apk
            is UpdateStage.NeedsPermission -> now.manifest to now.apk
            is UpdateStage.Failed -> (now.manifest ?: return) to (now.apk ?: return)
            else -> return
        }
        launchOnce { runInstall(manifest, apk) }
    }

    /** Back from Android settings: the permission may have been granted. */
    fun permissionChanged() {
        val now = state.value
        if (now is UpdateStage.NeedsPermission && installer.allowed()) state.value = UpdateStage.Ready(now.manifest, now.apk)
    }

    /** Install session outcome; false — not the current session, no need to open the confirmation window. */
    internal fun installed(session: Int, outcome: InstallOutcome): Boolean {
        while (true) {
            val now = state.value
            val next = afterInstall(now, session, outcome) ?: return false
            if (state.compareAndSet(now, next)) return true
        }
    }

    private suspend fun runCheck() {
        state.value = UpdateStage.Checking
        val manifest = attempt({ UpdateStage.Failed(it) }, BAD_MANIFEST) { fetchManifest(http) } ?: return
        val found = decide(installedCode(), manifest, Build.VERSION.SDK_INT)
        withContext(Dispatchers.IO) {
            saveLastManifest(manifest)
            if (found !is UpdateStage.Available) downloader.clear()
        }
        saveCheckTime(System.currentTimeMillis())
        state.value = found
    }

    private suspend fun runInstall(manifest: UpdateManifest, apk: File) {
        // a new APK would end the helper's recording: the download waits for it to finish
        if (recordingFolder() != null) {
            state.value = UpdateStage.Ready(manifest, apk)
            return
        }
        if (!installer.allowed()) {
            state.value = UpdateStage.NeedsPermission(manifest, apk)
            return
        }
        state.value = UpdateStage.Installing(manifest, apk)
        attempt({ UpdateStage.Failed(it, manifest, apk) }, INSTALL_FAILED) {
            withContext(Dispatchers.IO) {
                installer.install(apk) { session -> state.value = UpdateStage.Installing(manifest, apk, session) }
            }
        }
    }

    // a step failure is a line in "Update", not a process crash
    private suspend fun <T : Any> attempt(failed: (String) -> UpdateStage, fallback: String, step: suspend () -> T): T? = try {
        step()
    } catch (e: UpdateException) {
        state.value = failed(e.message ?: fallback)
        null
    } catch (e: CancellationException) {
        throw e
    } catch (e: Exception) {
        Log.w(LOG_TAG, fallback, e)
        state.value = failed(fallback)
        null
    }

    private fun saveLastManifest(manifest: UpdateManifest) {
        try {
            saveManifest(lastManifest, manifest)
        } catch (e: IOException) {
            Log.w(LOG_TAG, "Check manifest not saved", e)
        }
    }

    private suspend fun saveCheckTime(checkedAt: Long) {
        try {
            settings.update { it.copy(updateCheckedAt = checkedAt) }
        } catch (e: IOException) {
            Log.w(LOG_TAG, "Check time not saved", e)
        }
    }

    // buttons and the start call from the main thread: one action at a time
    private fun launchOnce(block: suspend () -> Unit) {
        if (work?.isActive == true) return
        work = scope.launch { block() }
    }

    private fun installedCode(): Long = packageInfo()?.longVersionCode ?: 0

    private fun packageInfo() = try {
        context.packageManager.getPackageInfo(context.packageName, PackageManager.PackageInfoFlags.of(0))
    } catch (e: PackageManager.NameNotFoundException) {
        Log.w(LOG_TAG, "App version not read", e)
        null
    }
}
