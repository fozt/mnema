package dev.mnema.app.ui.components

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.Stable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import java.io.File
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch

/** A recording for the player: [key] is the meta id or the path, [dir] and [title] are where it is now and what it is called. */
data class PlayingRecording(val key: String, val dir: File, val id: String?, val title: String)

data class Playing(val recording: PlayingRecording, val player: TrackPlayback)

/** Player of a recording whose header with the big player has scrolled off the top; [toTop] scrolls the recording back up. */
class CardMini(val recording: PlayingRecording, val player: TrackPlayback, val toTop: () -> Unit)

/** What the mini player above the tabs shows. */
sealed interface MiniShown {
    val title: String
    val player: TrackPlayback

    /** A recording playing outside its screen: a tap opens it, it can be closed. */
    data class Elsewhere(val playing: Playing) : MiniShown {
        override val title get() = playing.recording.title
        override val player get() = playing.player
    }

    /** Its own recording is scrolled: a tap returns to the header. */
    data class OnCard(val card: CardMini) : MiniShown {
        override val title get() = card.recording.title
        override val player get() = card.player
    }
}

/** One playing recording for the whole app: leaving its screen does not stop the sound. */
class NowPlayingModel(private val create: (List<File>) -> TrackPlayback) : ViewModel() {
    var playing by mutableStateOf<Playing?>(null)
        private set

    /** The recording whose screen is shown now: it needs no mini player. */
    var onCard by mutableStateOf<String?>(null)
        private set

    /** The shown recording is scrolled past its player. */
    var scrolled by mutableStateOf<CardMini?>(null)
        private set

    private var following: Job? = null
    private var cardOwner: Any? = null

    val miniPlayer: Playing? get() = playing?.takeIf { it.recording.key != onCard }

    /** Another recording is playing: its player; otherwise the shown recording's player while its header is off the top. */
    val miniShown: MiniShown?
        get() = miniPlayer?.let { MiniShown.Elsewhere(it) } ?: scrolled?.takeIf { it.recording.key == onCard }?.let { MiniShown.OnCard(it) }

    /** Player of the recording; any other recording stops. */
    fun activate(recording: PlayingRecording, files: List<File>): TrackPlayback {
        playing?.takeIf { it.recording.key == recording.key }?.let { return it.player }
        stop()
        val player = create(files)
        playing = Playing(recording, player)
        following = viewModelScope.launch { player.follow() }
        return player
    }

    /** The screen saw the recording again: new place and title; audio removed or tracks changed close the player. */
    fun update(recording: PlayingRecording, files: List<File>) {
        val current = playing?.takeIf { it.recording.key == recording.key } ?: return
        if (files.isEmpty() || current.player.labels.toSet() != files.map { it.nameWithoutExtension }.toSet()) {
            stop()
            return
        }
        current.player.relocate(files)
        if (current.recording != recording) playing = current.copy(recording = recording)
    }

    /** [owner] is the screen's own token: during an animation the new copy of the same screen enters before the old one leaves. */
    fun showing(key: String, owner: Any) {
        onCard = key
        cardOwner = owner
    }

    fun hidden(key: String, owner: Any) {
        if (cardOwner !== owner) return
        cardOwner = null
        if (onCard == key) onCard = null
        if (scrolled?.recording?.key == key) scrolled = null
    }

    fun scrolledAway(card: CardMini) {
        scrolled = card
    }

    fun scrolledBack(card: CardMini) {
        if (scrolled === card) scrolled = null
    }

    fun pause() {
        playing?.player?.pause()
    }

    fun stop(key: String) {
        if (playing?.recording?.key == key) stop()
    }

    fun stop() {
        following?.cancel()
        following = null
        playing?.player?.release()
        playing = null
    }

    override fun onCleared() = stop()
}

/**
 * Player of a recording screen: while another recording or nothing is playing it waits and starts on the first action:
 * Play, seeking or tapping an item.
 */
@Stable
class CardPlayback internal constructor(
    private val owner: NowPlayingModel,
    private val key: String,
    internal var recording: PlayingRecording,
    internal var files: List<File>,
    internal var lengthMs: Long,
) : TrackPlayback {
    private var waitingSpeed by mutableFloatStateOf(SPEEDS.first())
    private var waitingMuted by mutableStateOf(emptySet<String>())

    private val active: TrackPlayback? get() = owner.playing?.takeIf { it.recording.key == key }?.player

    override val labels: List<String> get() = active?.labels ?: files.map { it.nameWithoutExtension }
    override val playing: Boolean get() = active?.playing ?: false
    override val failed: Boolean get() = active?.failed ?: false
    override val durationMs: Long get() = active?.durationMs?.takeIf { it > 0 } ?: lengthMs
    override val positionMs: Long get() = active?.positionMs ?: 0L
    override val speed: Float get() = active?.speed ?: waitingSpeed
    override val muted: Set<String> get() = active?.muted ?: waitingMuted

    override fun toggle(play: Boolean) {
        if (play) start().toggle(true) else active?.toggle(false)
    }

    override fun pause() {
        active?.pause()
    }

    override fun seekTo(targetMs: Long) = start().seekTo(targetMs)

    override fun skip(deltaMs: Long) = start().skip(deltaMs)

    override fun selectSpeed(value: Float) {
        waitingSpeed = speedChoice(value)
        active?.selectSpeed(value)
    }

    override fun toggleTrack(label: String) {
        val player = active
        if (player == null) {
            waitingMuted = if (label in waitingMuted) waitingMuted - label else waitingMuted + label
        } else {
            player.toggleTrack(label)
            waitingMuted = player.muted
        }
    }

    // [NowPlayingModel] owns the track locations and the player's life
    override fun relocate(files: List<File>) = Unit

    override fun release() = Unit

    override suspend fun follow() = Unit

    private fun start(): TrackPlayback = active ?: owner.activate(recording, files).also { player ->
        player.selectSpeed(waitingSpeed)
        waitingMuted.forEach(player::toggleTrack)
    }
}

/** Player of a recording screen; null when there is no audio or the recording is still being made. */
@Composable
fun rememberCardPlayback(owner: NowPlayingModel, recording: PlayingRecording?, files: List<File>?, lengthMs: Long): TrackPlayback? {
    if (recording == null || files == null) return null
    LaunchedEffect(owner, recording, files) { owner.update(recording, files) }
    if (files.isEmpty()) return null
    val card = remember(owner, recording.key) { CardPlayback(owner, recording.key, recording, files, lengthMs) }
    SideEffect {
        card.recording = recording
        card.files = files
        card.lengthMs = lengthMs
    }
    return card
}
