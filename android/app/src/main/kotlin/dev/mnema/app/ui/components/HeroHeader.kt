package dev.mnema.app.ui.components

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.MaterialShapes
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.contentColorFor
import androidx.compose.material3.toShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.drawOutline
import androidx.compose.ui.graphics.drawscope.clipRect
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.graphics.drawscope.translate
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.dp

// Colored header under the status bar: the background goes under the system bar, the content stays below it
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun HeroHeader(
    modifier: Modifier = Modifier,
    container: Color = MaterialTheme.colorScheme.primaryContainer,
    decoration: Shape? = MaterialShapes.Sunny.toShape(),
    scrolled: (() -> Int)? = null,
    content: @Composable ColumnScope.() -> Unit,
) {
    val contentColor = contentColorFor(container)
    val statusTop = WindowInsets.statusBars.getTop(LocalDensity.current)
    Surface(modifier.fillMaxWidth(), shape = HeroShape, color = container, contentColor = contentColor) {
        Column(
            Modifier
                .fillMaxWidth()
                .decoration(decoration, contentColor.copy(alpha = DECORATION_ALPHA))
                .drawWithContent {
                    // The background and decoration stay under the clock; text and buttons do not go there.
                    if (scrolled == null) drawContent()
                    else clipRect(top = (statusTop + scrolled()).toFloat()) { this@drawWithContent.drawContent() }
                }
                .statusBarsPadding()
                .padding(start = 24.dp, end = 24.dp, top = 16.dp, bottom = 28.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
            content = content,
        )
    }
}

// A large translucent shape runs past the top right edge; the header clips it
private fun Modifier.decoration(shape: Shape?, color: Color): Modifier = if (shape == null) this else drawWithCache {
    val side = DECORATION_SIZE.toPx()
    val outline = shape.createOutline(Size(side, side), layoutDirection, this)
    onDrawBehind {
        translate(left = size.width - side * DECORATION_SHOWN, top = -side * (1 - DECORATION_SHOWN)) {
            rotate(DECORATION_TURN, pivot = Offset(side / 2, side / 2)) {
                drawOutline(outline, color)
            }
        }
    }
}

private val HeroShape = RoundedCornerShape(bottomStart = 28.dp, bottomEnd = 28.dp)
private val DECORATION_SIZE = 240.dp
private const val DECORATION_SHOWN = 0.7f
private const val DECORATION_TURN = 18f
private const val DECORATION_ALPHA = 0.1f
