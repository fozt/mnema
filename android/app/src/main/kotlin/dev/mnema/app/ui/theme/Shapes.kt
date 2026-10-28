package dev.mnema.app.ui.theme

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Shapes
import androidx.compose.material3.toPath
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.geometry.center
import androidx.compose.ui.graphics.Matrix
import androidx.compose.ui.graphics.Outline
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.LayoutDirection
import androidx.compose.ui.unit.dp
import androidx.graphics.shapes.Morph

val MnemaShapes = Shapes(extraLarge = RoundedCornerShape(28.dp))

// The shape reads progress and rotation on every outline build: the animation does not recompose the screen
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
class MorphShape(
    private val morph: Morph,
    private val progress: () -> Float,
    private val rotationDegrees: () -> Float = { 0f },
) : Shape {
    override fun createOutline(size: Size, layoutDirection: LayoutDirection, density: Density): Outline {
        val path = morph.toPath(progress(), Path())
        path.transform(Matrix().apply { scale(size.width, size.height) })
        path.translate(size.center - path.getBounds().center)
        val turn = rotationDegrees()
        if (turn != 0f) {
            val center = size.center
            path.transform(
                Matrix().apply {
                    translate(center.x, center.y)
                    rotateZ(turn)
                    translate(-center.x, -center.y)
                },
            )
        }
        return Outline.Generic(path)
    }
}
