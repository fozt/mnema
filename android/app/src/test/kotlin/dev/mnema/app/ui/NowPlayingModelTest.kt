package dev.mnema.app.ui

import androidx.lifecycle.ViewModelStore
import dev.mnema.app.helper.HelperStatus
import dev.mnema.app.ui.components.CardMini
import dev.mnema.app.ui.components.CardPlayback
import dev.mnema.app.ui.components.MiniShown
import dev.mnema.app.ui.components.NowPlayingModel
import dev.mnema.app.ui.components.PlayingRecording
import dev.mnema.app.ui.components.TrackPlayback
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class NowPlayingModelTest {
    private val created = mutableListOf<FakePlayback>()
    private val model = NowPlayingModel { FakePlayback(it).also(created::add) }
    private val a = PlayingRecording("a", File("a"), "a", "Дейли")
    private val b = PlayingRecording("b", File("b"), "b", "Звонок маме")
    private fun tracks(dir: File) = listOf(File(dir, "mic.wav"), File(dir, "system.wav"))
    private fun card(recording: PlayingRecording, lengthMs: Long = 90_000) =
        CardPlayback(model, recording.key, recording, tracks(recording.dir), lengthMs)
    private val cardA = Any()
    private val cardB = Any()

    @Before
    fun setUp() = Dispatchers.setMain(UnconfinedTestDispatcher())

    @After
    fun tearDown() = Dispatchers.resetMain()

    @Test
    fun tappingAPointStartsListeningFromThatMoment() {
        val card = card(a)
        assertFalse(card.playing)
        assertEquals(90_000L, card.durationMs)
        assertTrue("до первого действия плеер не создаётся", created.isEmpty())

        card.seekTo(42_000)
        card.toggle(true)

        val player = created.single()
        assertEquals(tracks(a.dir), player.files)
        assertEquals(42_000L, card.positionMs)
        assertTrue(card.playing)
        assertTrue(player.following)
        assertEquals(a, model.playing?.recording)
    }

    @Test
    fun leavingTheCardKeepsTheSoundAndShowsTheMiniPlayer() {
        val card = card(a)
        model.showing("a", cardA)
        card.toggle(true)
        assertNull("на своей карточке мини-плеер не нужен", model.miniPlayer)

        model.hidden("a", cardA)

        assertEquals(a, model.miniPlayer?.recording)
        assertTrue(model.miniPlayer!!.player.playing)
        assertEquals(0, created.single().released)
    }

    @Test
    fun anotherCardWaitsUntilItIsPlayed() {
        card(a).toggle(true)
        val other = card(b)
        model.showing("b", cardB)

        assertFalse(other.playing)
        assertEquals(a, model.miniPlayer?.recording)
        assertTrue(created.single().playing)

        other.toggle(true)

        assertEquals(1, created[0].released)
        assertFalse(created[0].following)
        assertEquals(b, model.playing?.recording)
        assertTrue(other.playing)
        assertNull(model.miniPlayer)
    }

    @Test
    fun speedAndMutedTrackChosenBeforePlayingAreKept() {
        val card = card(a)
        card.selectSpeed(1.5f)
        card.toggleTrack("system")
        assertEquals(1.5f, card.speed)
        assertEquals(setOf("system"), card.muted)

        card.toggle(true)

        assertEquals(1.5f, created.single().speed)
        assertEquals(setOf("system"), created.single().muted)
    }

    @Test
    fun renamedFolderMovesTheSamePlayer() {
        card(a).toggle(true)
        val renamed = a.copy(dir = File("2026-10-22_Дейли"), title = "Дейли с командой")

        model.update(renamed, tracks(renamed.dir))

        assertSame(created.single(), model.playing?.player)
        assertEquals(tracks(renamed.dir), created.single().files)
        assertEquals("Дейли с командой", model.playing?.recording?.title)
        assertTrue(created.single().playing)
    }

    @Test
    fun clearedSoundOrClosingStopsOnlyThatRecording() {
        card(a).toggle(true)
        model.update(b, emptyList())
        model.stop("b")
        assertEquals(a, model.playing?.recording)

        model.update(a, emptyList())

        assertNull(model.playing)
        assertEquals(1, created.single().released)
    }

    @Test
    fun pauseKeepsTheRecordingAndClearingTheOwnerReleasesIt() {
        val store = ViewModelStore().apply { put("now-playing", model) }
        card(a).toggle(true)

        model.pause()
        assertFalse(created.single().playing)
        assertEquals(a, model.playing?.recording)

        store.clear()
        assertEquals(1, created.single().released)
        assertFalse(created.single().following)
    }

    @Test
    fun scrolledCardShowsItsOwnMiniPlayerEvenBeforeListening() {
        var toTop = 0
        val card = card(a)
        model.showing("a", cardA)
        assertNull(model.miniShown)

        val mini = CardMini(a, card) { toTop++ }
        model.scrolledAway(mini)

        val shown = model.miniShown as MiniShown.OnCard
        assertEquals("Дейли", shown.title)
        assertFalse(shown.player.playing)
        shown.player.toggle(true)
        assertTrue(created.single().playing)
        assertTrue("своя запись звучит — мини-плеер остаётся своим", model.miniShown is MiniShown.OnCard)
        shown.card.toTop()
        assertEquals(1, toTop)

        model.scrolledBack(mini)
        assertNull(model.miniShown)
    }

    @Test
    fun anotherSoundingRecordingKeepsItsMiniPlayerAndLeavingTheCardForgetsTheScroll() {
        card(b).toggle(true)
        model.showing("a", cardA)
        model.scrolledAway(CardMini(a, card(a)) {})
        assertEquals(b, (model.miniShown as MiniShown.Elsewhere).playing.recording)

        model.stop()
        assertTrue(model.miniShown is MiniShown.OnCard)
        model.hidden("a", cardA)
        assertNull(model.scrolled)
        model.showing("b", cardB)
        model.scrolledAway(CardMini(a, card(a)) {})
        assertNull("прокрутка чужой карточки не в счёт", model.miniShown)
    }

    @Test
    fun reopenedCardStaysOnCardWhenItsLeavingCopyIsDisposedLater() {
        card(a).toggle(true)
        val leaving = Any()
        val entering = Any()
        model.showing("a", leaving)
        val oldMini = CardMini(a, card(a)) {}
        model.scrolledAway(oldMini)

        model.showing("a", entering)
        val newMini = CardMini(a, card(a)) {}
        model.scrolledAway(newMini)
        model.scrolledBack(oldMini)
        model.hidden("a", leaving)

        assertEquals("a", model.onCard)
        assertNull("своя карточка на экране — мини-плеера «где-то ещё» нет", model.miniPlayer)
        assertSame(newMini, (model.miniShown as MiniShown.OnCard).card)
        model.hidden("a", entering)
        assertNull(model.onCard)
        assertEquals(a, model.miniPlayer?.recording)
    }

    @Test
    fun startingARecordingPausesWhatPlaysButAnOngoingOneAtStartDoesNot() = runTest {
        fun status(recording: Boolean) = HelperStatus(v = 1, state = if (recording) "recording" else "idle")
        assertEquals(0, recordingStarts(flowOf(null, status(true), status(true))).toList().size)
        assertEquals(2, recordingStarts(flowOf(status(false), status(true), status(false), null, status(true))).toList().size)

        val card = card(a)
        card.toggle(true)
        pauseWhenRecordingStarts(flowOf(status(false), status(true)), model)

        assertFalse(created.single().playing)
        assertEquals("запись остаётся в плеере, только на паузе", a, model.playing?.recording)
    }

    private class FakePlayback(var files: List<File>) : TrackPlayback {
        override val labels get() = files.map { it.nameWithoutExtension }
        override var playing = false
        override val failed = false
        override val durationMs = 120_000L
        override var positionMs = 0L
        override var speed = 1f
        override var muted = emptySet<String>()
        var released = 0
        var following = false

        override fun toggle(play: Boolean) { playing = play }
        override fun pause() { playing = false }
        override fun seekTo(targetMs: Long) { positionMs = targetMs }
        override fun skip(deltaMs: Long) { positionMs += deltaMs }
        override fun selectSpeed(value: Float) { speed = value }
        override fun toggleTrack(label: String) { muted = if (label in muted) muted - label else muted + label }
        override fun relocate(files: List<File>) { this.files = files }
        override fun release() { released++ }
        override suspend fun follow() {
            following = true
            try { awaitCancellation() } finally { following = false }
        }
    }
}
