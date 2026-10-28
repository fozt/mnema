package dev.mnema.app.ui

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ViewList
import androidx.compose.material.icons.rounded.Mic
import androidx.compose.material.icons.rounded.Settings
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.navigation.NavController
import androidx.navigation.NavDestination.Companion.hierarchy
import dev.mnema.app.i18n.tr

/** Bottom tabs; [start] is the screen a tab begins with. */
enum class Tab(val route: String, val icon: ImageVector, val start: String = route) {
    RECORD("record", Icons.Rounded.Mic),
    RECORDINGS("recordings", Icons.AutoMirrored.Rounded.ViewList, start = "recordings/list"),
    SETTINGS("settings", Icons.Rounded.Settings),
    ;

    val title: String
        get() = when (this) {
            RECORD -> tr("Recorder")
            RECORDINGS -> tr("Recordings")
            SETTINGS -> tr("Settings")
        }
}

/** Pop the screens above [popTo], saving them if [keep], and show tab [show] as it was left. */
internal data class TabMove(val popTo: String, val keep: Boolean, val show: Tab? = null)

/**
 * A tap on tab [tapped], or a link to it, from the screen whose route chain is [current] (the screen and its graphs).
 * The open tab returns to its start; another one is shown over Record, so tabs never pile up:
 * Back from any of them leads to Record.
 */
internal fun tabMove(current: Sequence<String?>, tapped: Tab): TabMove = when {
    tabOf(current) == tapped -> TabMove(popTo = tapped.start, keep = false)
    tapped == Tab.RECORD -> TabMove(popTo = Tab.RECORD.route, keep = true)
    else -> TabMove(popTo = Tab.RECORD.route, keep = true, show = tapped)
}

internal fun tabOf(routes: Sequence<String?>): Tab? =
    routes.firstNotNullOfOrNull { route -> Tab.entries.firstOrNull { it.route == route } }

/** The bottom bar and links between tabs always navigate this way. */
fun NavController.showTab(tab: Tab) {
    val move = tabMove(currentDestination?.hierarchy?.map { it.route } ?: emptySequence(), tab)
    val shown = move.show
    if (shown == null) {
        popBackStack(move.popTo, inclusive = false, saveState = move.keep)
        return
    }
    navigate(shown.route) {
        popUpTo(move.popTo) { saveState = move.keep }
        launchSingleTop = true
        restoreState = true
    }
}

/** The tabs exist: the first launch starts with the wizard, without Record beneath it. */
internal fun NavController.hasTabs(): Boolean = runCatching { getBackStackEntry(Tab.RECORD.route) }.isSuccess
