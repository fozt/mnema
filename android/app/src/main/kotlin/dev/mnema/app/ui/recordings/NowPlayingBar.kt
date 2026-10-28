package dev.mnema.app.ui.recordings

import android.widget.Toast
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.platform.LocalContext
import dev.mnema.app.i18n.tr
import dev.mnema.app.recordings.RecordingItem
import dev.mnema.app.ui.components.MiniPlayer
import dev.mnema.app.ui.components.NowPlayingModel
import dev.mnema.app.ui.components.MiniShown
import dev.mnema.app.ui.components.PlayingRecording
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private val NOT_FOUND: String get() = tr("Recording not found")

/**
 * Mini player above the tabs; a tap opens the recording where it is now, even after a rename.
 * On a scrolled recording screen it is that screen's player: a tap returns to the header.
 */
@Composable
fun NowPlayingBar(root: File, nowPlaying: NowPlayingModel) {
    val shown = nowPlaying.miniShown
    // while the bar leaves, it keeps showing what was playing
    val last = remember { LastShown() }
    SideEffect { if (shown != null) last.shown = shown }
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    AnimatedVisibility(visible = shown != null, enter = expandVertically() + fadeIn(), exit = shrinkVertically() + fadeOut()) {
        when (val current = shown ?: last.shown ?: return@AnimatedVisibility) {
            is MiniShown.OnCard -> MiniPlayer(current.title, current.player, onOpen = current.card.toTop, onClose = null)
            is MiniShown.Elsewhere -> {
                val recording = current.playing.recording
                MiniPlayer(
                    title = current.title,
                    player = current.player,
                    onOpen = {
                        scope.launch {
                            val folder = withContext(Dispatchers.IO) { whereNow(root, recording) }
                            if (folder != null) {
                                RecordingLinks.open(folder.path)
                            } else {
                                nowPlaying.stop(recording.key)
                                Toast.makeText(context, NOT_FOUND, Toast.LENGTH_SHORT).show()
                            }
                        }
                    },
                    onClose = { nowPlaying.stop(recording.key) },
                )
            }
        }
    }
}

/** How the player recognizes a recording: the meta id survives a rename and a move to a section. */
fun playingKey(item: RecordingItem): String = item.id ?: item.dir.absolutePath

private class LastShown {
    var shown: MiniShown? = null
}

private fun whereNow(root: File, recording: PlayingRecording): File? = FolderFollower(root, recording.dir, recording = recording.id).current()
