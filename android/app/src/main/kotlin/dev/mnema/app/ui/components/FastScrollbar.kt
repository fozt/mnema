package dev.mnema.app.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.ProgressBarRangeInfo
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.progressBarRangeInfo
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.setProgress
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import dev.mnema.app.i18n.tr
import kotlin.math.roundToInt
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.receiveAsFlow

/** Dragging along the right edge moves quickly through a long conversation. */
@Composable
fun FastScrollbar(list: LazyListState, enabled: Boolean, modifier: Modifier = Modifier) {
    var recent by remember(list) { mutableStateOf(false) }
    var dragging by remember(list) { mutableStateOf(false) }
    var dragged by remember(list) { mutableFloatStateOf(0f) }
    var height by remember { mutableIntStateOf(0) }
    val thumbHeight = with(LocalDensity.current) { 48.dp.toPx() }
    val travel = (height - thumbHeight).coerceAtLeast(1f)
    val targets = remember(list) { Channel<Int>(Channel.CONFLATED) }
    val longList by remember(list) {
        derivedStateOf {
            val info = list.layoutInfo
            info.visibleItemsInfo.isNotEmpty() && info.totalItemsCount > info.visibleItemsInfo.size * 4
        }
    }
    val fraction by remember(list) {
        derivedStateOf {
            val info = list.layoutInfo
            scrollFraction(
                list.firstVisibleItemIndex, list.firstVisibleItemScrollOffset,
                info.visibleItemsInfo.firstOrNull { it.index == list.firstVisibleItemIndex }?.size ?: 0,
                info.totalItemsCount, info.visibleItemsInfo.size, list.canScrollForward,
            )
        }
    }
    LaunchedEffect(list) {
        snapshotFlow { list.isScrollInProgress }.collectLatest { scrolling ->
            if (scrolling) recent = true else {
                delay(1_500)
                recent = false
            }
        }
    }
    LaunchedEffect(list) {
        targets.receiveAsFlow().collectLatest { list.scrollToItem(it) }
    }
    val visible = dragging || (enabled && longList && recent)
    val shown = if (dragging) dragged else fraction
    val seek = { target: Float ->
        val info = list.layoutInfo
        targets.trySend(scrollPosition(target, info.totalItemsCount, info.visibleItemsInfo.size))
        Unit
    }
    Box(modifier.fillMaxHeight().width(24.dp).onSizeChanged { height = it.height }) {
        if (visible) {
            Box(
                Modifier
                    .fillMaxHeight()
                    .width(24.dp)
                    .semantics {
                        contentDescription = tr("Conversation scroll")
                        progressBarRangeInfo = ProgressBarRangeInfo(shown, 0f..1f)
                        setProgress { seek(it); true }
                    }
                    .pointerInput(list, travel) {
                        var startY = 0f
                        var startFraction = 0f
                        detectVerticalDragGestures(
                            onDragStart = {
                                startY = it.y
                                val thumbTop = fraction * travel
                                startFraction = if (it.y in thumbTop..(thumbTop + thumbHeight)) fraction
                                else ((it.y - thumbHeight / 2) / travel).coerceIn(0f, 1f)
                                dragged = startFraction
                                dragging = true
                                seek(dragged)
                            },
                            onDragEnd = { dragging = false },
                            onDragCancel = { dragging = false },
                        ) { change, _ ->
                            change.consume()
                            dragged = (startFraction + (change.position.y - startY) / travel).coerceIn(0f, 1f)
                            seek(dragged)
                        }
                    },
            ) {
                Box(
                    Modifier
                        .align(Alignment.TopCenter)
                        .offset { IntOffset(0, (shown * travel).roundToInt()) }
                        .size(width = 6.dp, height = 48.dp)
                        .background(MaterialTheme.colorScheme.primary, CircleShape),
                )
            }
        }
    }
}
