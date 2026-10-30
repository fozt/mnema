package dev.mnema.app

import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.View
import android.view.ViewTreeObserver
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.lifecycleScope
import dev.mnema.app.helper.HelperProtocol
import dev.mnema.app.i18n.tr
import dev.mnema.app.notify.Notifications
import dev.mnema.app.settings.ColorSource
import dev.mnema.app.settings.ThemeMode
import dev.mnema.app.ui.MnemaNav
import dev.mnema.app.ui.recordings.FolderFollower
import dev.mnema.app.ui.recordings.RecordingLinks
import dev.mnema.app.ui.theme.MnemaTheme
import dev.mnema.app.ui.theme.isDark
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val graph = MnemaApp.graph(this)
        val look: StateFlow<Look?> = graph.settings.settings
            .map { Look(it.themeMode, it.colorSource) }
            .stateIn(lifecycleScope, SharingStarted.Eagerly, null)
        holdFirstFrameUntil { look.value != null }
        setContent {
            val shown by look.collectAsStateWithLifecycle()
            shown?.let { current ->
                val dark = isDark(current.mode, isSystemInDarkTheme())
                DisposableEffect(dark) {
                    val bars = SystemBarStyle.auto(Color.TRANSPARENT, Color.TRANSPARENT) { dark }
                    enableEdgeToEdge(statusBarStyle = bars, navigationBarStyle = bars)
                    onDispose {}
                }
                MnemaTheme(current.mode, dynamicColor = current.colors == ColorSource.WALLPAPER) { MnemaNav(graph) }
            }
        }
        if (savedInstanceState == null) openRecordingFrom(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        openRecordingFrom(intent)
    }

    override fun onResume() {
        super.onResume()
        Notifications.syncWithStatus(this)
        MnemaApp.graph(this).updates.checkIfDue()
    }

    // Recording notification; from Recents the old intent arrives again — it is not opened
    private fun openRecordingFrom(intent: Intent) {
        if (intent.flags and Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY != 0) return
        val path = intent.getStringExtra(Notifications.EXTRA_FOLDER) ?: return
        val id = intent.getStringExtra(Notifications.EXTRA_ID)
        val fileKey = intent.getStringExtra(Notifications.EXTRA_FILE_KEY)
        if (id == null && fileKey == null) return
        // the folder may have been renamed after the notification: then it is found by its library id
        lifecycleScope.launch {
            val folder = withContext(Dispatchers.IO) {
                FolderFollower(MnemaApp.graph(this@MainActivity).recordingsRoot, File(path), recording = id, snapshotKey = fileKey).current()
            }
            if (folder != null) RecordingLinks.open(folder.path)
            else android.widget.Toast.makeText(this@MainActivity, tr("Recording not found"), android.widget.Toast.LENGTH_SHORT).show()
        }
    }

    private data class Look(val mode: ThemeMode, val colors: ColorSource)

    // until the theme is read from settings the splash screen stays: no flash of a wrong theme
    private fun holdFirstFrameUntil(ready: () -> Boolean) {
        val content = findViewById<View>(android.R.id.content)
        content.viewTreeObserver.addOnPreDrawListener(
            object : ViewTreeObserver.OnPreDrawListener {
                override fun onPreDraw(): Boolean {
                    if (!ready()) return false
                    content.viewTreeObserver.removeOnPreDrawListener(this)
                    return true
                }
            },
        )
    }
}
