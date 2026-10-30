package dev.mnema.app.ui

import androidx.activity.compose.LocalActivity
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Badge
import androidx.compose.material3.BadgedBox
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.ShortNavigationBar
import androidx.compose.material3.ShortNavigationBarItem
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleStartEffect
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavBackStackEntry
import androidx.navigation.NavDestination.Companion.hierarchy
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import dev.mnema.app.AppGraph
import dev.mnema.app.helper.HelperClient
import dev.mnema.app.helper.HelperStatus
import dev.mnema.app.i18n.tr
import dev.mnema.app.ui.components.NowPlayingModel
import dev.mnema.app.ui.components.TrackPlayer
import dev.mnema.app.ui.onboarding.OnboardingScreen
import dev.mnema.app.ui.record.RecordScreen
import dev.mnema.app.ui.record.helperClient
import dev.mnema.app.ui.recordings.NowPlayingBar
import dev.mnema.app.ui.recordings.OpenRequestedRecordings
import dev.mnema.app.ui.recordings.recordingsGraph
import dev.mnema.app.ui.settings.SettingsScreen
import dev.mnema.app.ui.theme.MnemaMotion
import dev.mnema.app.update.updatePending
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.flow.map

private const val ONBOARDING_ROUTE = "onboarding"

@Composable
fun MnemaNav(graph: AppGraph) {
    val onboardingDone by remember(graph) { graph.settings.settings.map<_, Boolean?> { it.onboardingDone } }
        .collectAsStateWithLifecycle(initialValue = null)
    // while settings load the screen is empty, so the first launch does not flash the Record tab
    val done = onboardingDone ?: return
    val nowPlaying = rememberNowPlaying(graph)
    val update by graph.updates.stage.collectAsStateWithLifecycle()
    val start = rememberSaveable { if (done) Tab.RECORD.route else ONBOARDING_ROUTE }
    val navController = rememberNavController()
    val backStackEntry by navController.currentBackStackEntryAsState()
    val destination = backStackEntry?.destination
    val motion = MaterialTheme.motionScheme
    val offset = with(LocalDensity.current) { MnemaMotion.SharedAxisOffset.roundToPx() }

    // screens keep clear of the status bar themselves: their top bars extend under it
    Scaffold(
        contentWindowInsets = WindowInsets(0),
        bottomBar = {
            if (destination?.route != ONBOARDING_ROUTE) Column {
                NowPlayingBar(graph.recordingsRoot, nowPlaying)
                ShortNavigationBar {
                    Tab.entries.forEach { tab ->
                        ShortNavigationBarItem(
                            selected = destination?.hierarchy?.any { it.route == tab.route } == true,
                            onClick = { navController.showTab(tab) },
                            icon = { TabIcon(tab, dot = tab == Tab.SETTINGS && updatePending(update)) },
                            label = { Text(tab.title) },
                        )
                    }
                }
            }
        },
    ) { padding ->
        NavHost(
            navController = navController,
            startDestination = start,
            modifier = Modifier.padding(padding).consumeWindowInsets(padding),
            enterTransition = { MnemaMotion.sharedAxisEnter(motion, offset, forward(initialState, targetState, true)) },
            exitTransition = { MnemaMotion.sharedAxisExit(motion, offset, forward(initialState, targetState, true)) },
            popEnterTransition = { MnemaMotion.sharedAxisEnter(motion, offset, forward(initialState, targetState, false)) },
            popExitTransition = { MnemaMotion.sharedAxisExit(motion, offset, forward(initialState, targetState, false)) },
        ) {
            composable(ONBOARDING_ROUTE) {
                val opened = navController.previousBackStackEntry != null
                OnboardingScreen(
                    graph,
                    onDone = {
                        navController.navigate(Tab.RECORD.route) {
                            popUpTo(ONBOARDING_ROUTE) { inclusive = true }
                            launchSingleTop = true
                        }
                    },
                    onBack = if (opened) ({ navController.popBackStack() }) else null,
                )
            }
            composable(Tab.RECORD.route) {
                RecordScreen(graph, onSetUpHelper = {
                    navController.navigate(ONBOARDING_ROUTE) { launchSingleTop = true }
                }, onRecord = nowPlaying::pause)
            }
            recordingsGraph(graph, navController, nowPlaying)
            composable(Tab.SETTINGS.route) { SettingsScreen(graph) }
        }
        OpenRequestedRecordings(navController)
    }
}

@Composable
private fun TabIcon(tab: Tab, dot: Boolean) {
    val available = tr("Update available")
    BadgedBox(badge = { if (dot) Badge(Modifier.semantics { contentDescription = available }) }) {
        Icon(tab.icon, contentDescription = null)
    }
}

// Playback lives with the Activity: moving between screens leaves it alone, going to the background or starting a recording pauses it.
@Composable
private fun rememberNowPlaying(graph: AppGraph): NowPlayingModel {
    val application = LocalContext.current.applicationContext
    val activity = LocalActivity.current
    val nowPlaying = viewModel { NowPlayingModel { TrackPlayer(application, it) } }
    LifecycleStartEffect(nowPlaying, activity) {
        onStopOrDispose {
            // rotation and theme changes recreate the Activity while the user stays put
            if (activity?.isChangingConfigurations != true) nowPlaying.pause()
        }
    }
    // a recording started from the notification or by a call: the playing recording must not leak into it
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    LaunchedEffect(graph, nowPlaying, lifecycle) {
        val helper = helperClient(graph).getOrNull() ?: return@LaunchedEffect
        lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
            // the helper heartbeat is read only while the player holds a recording
            snapshotFlow { nowPlaying.playing != null }.collectLatest { loaded ->
                if (loaded) pauseWhenRecordingStarts(helperStatuses(helper), nowPlaying)
            }
        }
    }
    return nowPlaying
}

private fun helperStatuses(helper: HelperClient): Flow<HelperStatus?> = flow {
    while (true) {
        emit(helper.readStatus())
        delay(STATUS_POLL_MS)
    }
}.flowOn(Dispatchers.IO)

private const val STATUS_POLL_MS = 500L

/** Every recording start after a "not recording" was read: a recording already under way is not a start. */
internal fun recordingStarts(statuses: Flow<HelperStatus?>): Flow<Unit> = flow {
    var before: Boolean? = null
    statuses.collect { status ->
        val now = status?.recording ?: return@collect
        if (now && before == false) emit(Unit)
        before = now
    }
}

internal suspend fun pauseWhenRecordingStarts(statuses: Flow<HelperStatus?>, nowPlaying: NowPlayingModel) =
    recordingStarts(statuses).collect { nowPlaying.pause() }

private fun forward(from: NavBackStackEntry, to: NavBackStackEntry, otherwise: Boolean): Boolean =
    tabForward(from.destination.hierarchy.map { it.route }, to.destination.hierarchy.map { it.route }, otherwise)

// Between tabs their order sets the direction; otherwise it is going deeper or back
internal fun tabForward(from: Sequence<String?>, to: Sequence<String?>, otherwise: Boolean): Boolean {
    val fromTab = tabOf(from) ?: return otherwise
    val toTab = tabOf(to) ?: return otherwise
    return if (fromTab == toTab) otherwise else toTab > fromTab
}
