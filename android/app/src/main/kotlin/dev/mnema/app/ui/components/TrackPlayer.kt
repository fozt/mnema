package dev.mnema.app.ui.components

import android.content.Context
import android.net.Uri
import androidx.compose.runtime.Stable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackParameters
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import java.io.File
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest

// Each track is its own ExoPlayer; the lead is the longest one: it holds audio focus and the others follow it
@Stable
class TrackPlayer(context: Context, files: List<File>) : TrackPlayback {
    private val tracks: List<Pair<String, ExoPlayer>> = files.sortedByDescending { it.length() }
        .mapIndexed { index, file -> file.nameWithoutExtension to build(context, file, leads = index == 0) }
    private val leader = tracks.first().second
    private val followers = tracks.drop(1).map { it.second }

    /** Track names: `mic`, `system`. */
    override val labels: List<String> = tracks.map { it.first }
    private var sources = files.associate { it.nameWithoutExtension to it.absolutePath }

    override var playing by mutableStateOf(false)
        private set
    override var failed by mutableStateOf(false)
        private set
    override var durationMs by mutableLongStateOf(0L)
        private set
    override var positionMs by mutableLongStateOf(0L)
        private set
    override var speed by mutableFloatStateOf(1f)
        private set
    override var muted by mutableStateOf(emptySet<String>())
        private set

    private val listener = object : Player.Listener {
        override fun onEvents(player: Player, events: Player.Events) {
            mirror()
            refresh()
        }
    }

    init {
        tracks.forEach { (_, player) -> player.addListener(listener) }
    }

    override fun toggle(play: Boolean) {
        if (play && leader.playbackState == Player.STATE_ENDED) seekTo(0)
        leader.playWhenReady = play
        mirror()
        refresh()
    }

    override fun pause() = toggle(false)

    // The length is not read yet when the player was just created by tapping an item: then the position has no upper bound.
    override fun seekTo(targetMs: Long) {
        val target = if (durationMs > 0) targetMs.coerceIn(0L, durationMs) else targetMs.coerceAtLeast(0L)
        tracks.forEach { (_, player) -> player.seekTo(lengthOf(player).takeIf { it > 0 }?.let { minOf(target, it) } ?: target) }
        positionMs = target
    }

    override fun skip(deltaMs: Long) = seekTo(if (durationMs > 0) skipTarget(position(), deltaMs, durationMs) else position() + deltaMs)

    // pitch stays the same: PlaybackParameters(speed) keeps pitch = 1
    override fun selectSpeed(value: Float) {
        val chosen = speedChoice(value)
        if (chosen == speed) return
        speed = chosen
        val parameters = PlaybackParameters(chosen)
        tracks.forEach { (_, player) -> player.playbackParameters = parameters }
    }

    override fun toggleTrack(label: String) {
        muted = if (label in muted) muted - label else muted + label
        tracks.forEach { (name, player) -> player.volume = if (name in muted) 0f else 1f }
    }

    override fun relocate(files: List<File>) {
        val next = files.associate { it.nameWithoutExtension to it.absolutePath }
        if (next == sources) return
        val at = position()
        val play = leader.playWhenReady
        for ((label, player) in tracks) {
            val path = next[label] ?: continue
            if (path == sources[label]) continue
            // An open descriptor survives a rename; reading again on seek already uses the new path.
            val target = lengthOf(player).takeIf { it > 0 }?.let { minOf(at, it) } ?: at
            player.setMediaItem(MediaItem.fromUri(Uri.fromFile(File(path))), target)
            player.prepare()
            player.playWhenReady = play
        }
        sources = next
        refresh()
    }

    override fun release() {
        tracks.forEach { (_, player) ->
            player.removeListener(listener)
            player.release()
        }
    }

    /** While audio plays: the position for the bar and a track sync once a second. */
    override suspend fun follow() {
        snapshotFlow { playing }.collectLatest { on ->
            if (!on) return@collectLatest
            var sinceSync = 0L
            while (true) {
                positionMs = position()
                if (sinceSync >= SYNC_MS) {
                    resync()
                    sinceSync = 0L
                }
                delay(TICK_MS)
                sinceSync += TICK_MS
            }
        }
    }

    // a pause of the lead for focus, headphones or a call pauses them all
    private fun mirror() {
        val go = leader.playWhenReady && leader.playbackSuppressionReason == Player.PLAYBACK_SUPPRESSION_REASON_NONE &&
            leader.playerError == null
        followers.forEach { it.playWhenReady = go }
    }

    private fun refresh() {
        val state = leader.playbackState
        playing = leader.playWhenReady && leader.playbackSuppressionReason == Player.PLAYBACK_SUPPRESSION_REASON_NONE &&
            state != Player.STATE_IDLE && state != Player.STATE_ENDED
        failed = leader.playerError != null
        durationMs = tracks.maxOf { lengthOf(it.second) }
        positionMs = position()
    }

    private fun resync() {
        if (leader.playbackState != Player.STATE_READY) return
        val lead = leader.currentPosition
        for (follower in followers) {
            if (follower.playbackState != Player.STATE_READY) continue
            resyncTarget(lead, follower.currentPosition, lengthOf(follower))?.let { follower.seekTo(it) }
        }
    }

    private fun position(): Long = if (leader.playbackState == Player.STATE_ENDED) durationMs else leader.currentPosition

    private companion object {
        const val TICK_MS = 200L
        val SPEECH: AudioAttributes = AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_SPEECH).build()

        fun lengthOf(player: Player): Long = player.duration.takeIf { it != C.TIME_UNSET && it > 0 } ?: 0L

        // audio focus and "headphones unplugged" only for the lead: otherwise the tracks would take focus from each other
        fun build(context: Context, file: File, leads: Boolean): ExoPlayer = ExoPlayer.Builder(context)
            .setAudioAttributes(SPEECH, leads)
            .setHandleAudioBecomingNoisy(leads)
            .build()
            .apply {
                setMediaItem(MediaItem.fromUri(Uri.fromFile(file)))
                prepare()
            }
    }
}
