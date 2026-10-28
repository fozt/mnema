package dev.mnema.app

import dev.mnema.app.recordings.MediaIndex
import android.app.Application
import android.content.Context
import android.content.res.Configuration
import android.util.Log
import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.Redact
import dev.mnema.app.i18n.Ui
import dev.mnema.app.i18n.UiLang
import dev.mnema.app.notify.Notifications
import kotlinx.coroutines.launch

class MnemaApp : Application() {
    val graph: AppGraph by lazy { AppGraph(this) }

    @Volatile
    private var secrets: List<String> = emptyList()

    override fun onCreate() {
        super.onCreate()
        Ui.lang = languageOf(resources.configuration)
        ensureControlDir()
        Redact.install { secrets }
        graph.scope.launch { graph.settings.settings.collect { secrets = secretsOf(it) } }
        Notifications.ensureChannels(this)
        MediaIndex.scanner = { files, wait -> scanFiles(files, wait) }
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        val lang = languageOf(newConfig)
        if (lang == Ui.lang) return
        Ui.lang = lang
        // Channel names are shown in system settings; re-creating a channel renames it
        Notifications.ensureChannels(this)
    }

    private fun languageOf(configuration: Configuration): UiLang = UiLang.of(configuration.locales[0]?.language)

    // MediaScannerConnection: wait for the answer only before retrying a move, at most 10 s
    private fun scanFiles(files: List<java.io.File>, wait: Boolean) {
        if (files.isEmpty()) return
        val done = java.util.concurrent.CountDownLatch(files.size)
        android.media.MediaScannerConnection.scanFile(this, files.map { it.absolutePath }.toTypedArray(), null) { _, _ -> done.countDown() }
        if (wait) done.await(10, java.util.concurrent.TimeUnit.SECONDS)
    }

    // only the app creates the exchange folder: one created by shell has another owner
    private fun ensureControlDir() {
        val created = try {
            getExternalFilesDir(HelperProtocol.CONTROL_DIR)
        } catch (e: Exception) {
            Log.w(LOG_TAG, NO_CONTROL_DIR, e)
            return
        }
        if (created == null) Log.w(LOG_TAG, NO_CONTROL_DIR)
    }

    companion object {
        private const val LOG_TAG = "MnemaApp"
        private const val NO_CONTROL_DIR = "Helper exchange folder not created: shared storage unavailable"

        fun graph(context: Context): AppGraph = (context.applicationContext as MnemaApp).graph
    }
}
