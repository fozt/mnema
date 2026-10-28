package dev.mnema.app.ui.components

import java.io.File

/** Boundary with the audio player: state and control of one recording's tracks. */
interface TrackPlayback {
    val labels: List<String>
    val playing: Boolean
    val failed: Boolean
    val durationMs: Long
    val positionMs: Long
    val speed: Float
    val muted: Set<String>
    fun toggle(play: Boolean)
    fun pause()
    fun seekTo(targetMs: Long)
    fun skip(deltaMs: Long)
    fun selectSpeed(value: Float)
    fun toggleTrack(label: String)
    fun relocate(files: List<File>)
    fun release()
    suspend fun follow()
}
