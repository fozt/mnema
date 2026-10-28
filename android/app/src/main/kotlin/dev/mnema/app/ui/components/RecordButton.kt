package dev.mnema.app.ui.components

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Mic
import androidx.compose.material.icons.rounded.Stop
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ripple
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import androidx.graphics.shapes.Morph
import dev.mnema.app.i18n.tr
import dev.mnema.app.ui.theme.MnemaMotion
import dev.mnema.app.ui.theme.MorphShape
import kotlin.math.ceil

private val RECORD_LABEL: String get() = tr("Start recording")
private val STOP_LABEL: String get() = tr("Stop recording")

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun RecordButton(recording: Boolean, enabled: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    val motion = MaterialTheme.motionScheme
    val colors = MaterialTheme.colorScheme
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val morphProgress by animateFloatAsState(if (pressed) 1f else 0f, motion.fastSpatialSpec(), label = "morph")
    val rotation = remember { Animatable(0f) }
    LaunchedEffect(recording) {
        if (recording) {
            while (true) {
                rotation.animateTo(rotation.value + 360f, tween(MnemaMotion.RECORD_TURN_MS, easing = LinearEasing))
            }
        } else {
            // the cookie has 12 equal teeth: keep turning forward to the nearest one
            rotation.animateTo(ceil(rotation.value / COOKIE_STEP) * COOKIE_STEP, motion.fastSpatialSpec())
        }
    }
    val container by animateColorAsState(
        when {
            !enabled -> colors.onSurface.copy(alpha = DISABLED_CONTAINER)
            recording -> colors.error
            else -> colors.primary
        },
        motion.slowEffectsSpec(),
        label = "container",
    )
    val content by animateColorAsState(
        when {
            !enabled -> colors.onSurface.copy(alpha = DISABLED_CONTENT)
            recording -> colors.onError
            else -> colors.onPrimary
        },
        motion.slowEffectsSpec(),
        label = "content",
    )
    val shape = remember {
        MorphShape(Morph(MaterialShapes.Cookie12Sided, MaterialShapes.Square), { morphProgress }, { rotation.value })
    }

    Box(modifier.size(OUTER_SIZE), contentAlignment = Alignment.Center) {
        AnimatedVisibility(recording, enter = fadeIn(motion.slowEffectsSpec()), exit = fadeOut(motion.slowEffectsSpec())) {
            PulseRing(colors.error, Modifier.fillMaxSize())
        }
        Box(
            Modifier
                .fillMaxSize(BODY_FRACTION)
                .clip(shape)
                .background(container)
                .clickable(
                    interactionSource = interaction,
                    indication = ripple(),
                    enabled = enabled,
                    role = Role.Button,
                    onClick = onClick,
                ),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                if (recording) Icons.Rounded.Stop else Icons.Rounded.Mic,
                contentDescription = if (recording) STOP_LABEL else RECORD_LABEL,
                modifier = Modifier.fillMaxSize(ICON_FRACTION),
                tint = content,
            )
        }
    }
}

// A ring spreads from the button edge and fades
@Composable
private fun PulseRing(color: Color, modifier: Modifier) {
    val pulse = rememberInfiniteTransition(label = "pulse")
    val phase by pulse.animateFloat(
        initialValue = 0f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(MnemaMotion.PULSE_MS), RepeatMode.Restart),
        label = "phase",
    )
    Canvas(modifier) {
        val outer = size.minDimension / 2
        val inner = outer * BODY_FRACTION
        drawCircle(color.copy(alpha = RING_ALPHA * (1f - phase)), radius = inner + (outer - inner) * phase)
    }
}

private const val COOKIE_STEP = 30f
private const val RING_ALPHA = 0.35f
private const val BODY_FRACTION = 0.8f
private const val ICON_FRACTION = 0.3f
private const val DISABLED_CONTAINER = 0.12f
private const val DISABLED_CONTENT = 0.38f
private val OUTER_SIZE = 200.dp
