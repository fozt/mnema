package dev.mnema.app.ui.record

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.ContentTransform
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MotionScheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.toShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import dev.mnema.app.i18n.RU_MONTHS_OF
import dev.mnema.app.i18n.RU_WEEKDAYS
import dev.mnema.app.i18n.Ui
import dev.mnema.app.i18n.UiLang
import dev.mnema.app.ui.components.HeroHeader
import java.time.LocalDate
import java.time.format.TextStyle
import java.util.Locale

/** Date in the Record header: "Thursday, October 22". */
fun heroDate(day: LocalDate): String = when (Ui.lang) {
    UiLang.RU -> "${RU_WEEKDAYS[day.dayOfWeek.value - 1]}, ${day.dayOfMonth} ${RU_MONTHS_OF[day.monthValue - 1]}"
    UiLang.EN -> "${day.dayOfWeek.getDisplayName(TextStyle.FULL, Locale.ENGLISH)}, " +
        "${day.month.getDisplayName(TextStyle.FULL, Locale.ENGLISH)} ${day.dayOfMonth}"
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
internal fun RecordHero(date: String, title: String, chip: String?, dot: Color, recording: Boolean, modifier: Modifier = Modifier) {
    // while recording the decoration is the same cookie as the button
    val decoration = (if (recording) MaterialShapes.Cookie12Sided else MaterialShapes.Sunny).toShape()
    val motion = MaterialTheme.motionScheme
    HeroHeader(modifier, decoration = decoration) {
        Text(date, style = MaterialTheme.typography.labelLarge, color = LocalContentColor.current.copy(alpha = DATE_ALPHA))
        AnimatedContent(title, transitionSpec = { fade(motion) }, label = "title") { shown ->
            Text(shown, Modifier.semantics { heading() }, style = MaterialTheme.typography.displaySmallEmphasized)
        }
        AnimatedContent(chip, transitionSpec = { fade(motion) }, label = "chip") { shown ->
            if (shown != null) HelperChip(shown, dot, Modifier.padding(top = 4.dp))
        }
    }
}

private fun fade(motion: MotionScheme): ContentTransform =
    fadeIn(motion.defaultEffectsSpec()) togetherWith fadeOut(motion.fastEffectsSpec())

@Composable
private fun HelperChip(text: String, dot: Color, modifier: Modifier = Modifier) {
    Surface(
        modifier,
        shape = CircleShape,
        color = MaterialTheme.colorScheme.surfaceContainerLowest,
        contentColor = MaterialTheme.colorScheme.onSurface,
    ) {
        Row(
            Modifier.padding(start = 12.dp, end = 16.dp, top = 6.dp, bottom = 6.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(Modifier.size(DOT_SIZE).background(dot, CircleShape))
            Text(text, style = MaterialTheme.typography.labelLarge)
        }
    }
}

private const val DATE_ALPHA = 0.8f
private val DOT_SIZE = 10.dp
