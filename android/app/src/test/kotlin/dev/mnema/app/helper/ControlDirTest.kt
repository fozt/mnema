package dev.mnema.app.helper

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class ControlDirTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val dir get() = File(folder.root, "control")
    private val control get() = ControlDir(dir)

    @Test
    fun missingDirIsNeverCreated() {
        val control = control

        for (second in 0..130) control.ready(second * 1000L)

        assertFalse(dir.exists())
    }

    @Test
    fun missingDirIsCheckedOnceInTwoSeconds() {
        val control = control
        assertFalse(control.ready(10_000))

        dir.mkdir()

        assertFalse("just checked", control.ready(10_000 + ControlDir.CHECK_MS - 1))
        assertTrue(control.ready(10_000 + ControlDir.CHECK_MS))
    }

    @Test
    fun lostDirIsNoticedWithoutWaiting() {
        dir.mkdir()
        val control = control
        assertTrue(control.ready(10_000))

        dir.delete()

        assertFalse(control.ready(10_300))
    }

    @Test
    fun reminderIsDueAtOnceThenOncePerMinute() {
        val control = control
        control.ready(10_000)

        assertTrue(control.reminderDue(10_000))
        assertFalse(control.reminderDue(10_000 + ControlDir.REMIND_MS - 1))
        assertTrue(control.reminderDue(10_000 + ControlDir.REMIND_MS))
        assertFalse(control.reminderDue(10_000 + ControlDir.REMIND_MS + 1))
    }

    @Test
    fun reminderIsNotDueWhileDirExists() {
        dir.mkdir()
        val control = control
        control.ready(10_000)

        assertFalse(control.reminderDue(10_000))
        assertFalse(control.reminderDue(10_000 + ControlDir.REMIND_MS))
    }

    @Test
    fun reminderRestartsWhenDirIsLostAgain() {
        dir.mkdir()
        val control = control
        control.ready(10_000)
        dir.delete()
        control.ready(10_300)
        assertTrue(control.reminderDue(10_300))

        dir.mkdir()
        assertTrue(control.ready(10_300 + ControlDir.CHECK_MS))
        dir.delete()
        assertFalse(control.ready(13_000))

        assertTrue("a new loss is reported at once", control.reminderDue(13_000))
    }
}
