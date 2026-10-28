package dev.mnema.app.ui.components

import kotlin.math.roundToInt

/** Approximate position by lines: paragraph heights are not known in advance. */
fun scrollFraction(first: Int, offset: Int, firstSize: Int, total: Int, visible: Int, canForward: Boolean): Float {
    if (total <= visible || total <= 1) return 0f
    if (!canForward) return 1f
    val partial = if (firstSize > 0) offset.toFloat() / firstSize else 0f
    return ((first + partial) / (total - visible).coerceAtLeast(1)).coerceIn(0f, 1f)
}

/** The end of the bar always leads to the last line, even one taller than the screen. */
fun scrollPosition(fraction: Float, total: Int, visible: Int): Int {
    if (total <= 1 || !fraction.isFinite()) return 0
    if (fraction >= 1f) return total - 1
    return (fraction.coerceAtLeast(0f) * (total - visible).coerceAtLeast(0)).roundToInt().coerceIn(0, total - 1)
}

/** "To top" is shown once the whole first screen has scrolled up: [firstScreen] is how many items fit on it. */
fun showsToTop(first: Int, firstScreen: Int): Boolean = firstScreen > 0 && first >= firstScreen

/** The collapsed header appears before the colored background leaves the status bar. */
fun headerCollapsed(first: Int, heroBottom: Int?, barBottom: Int): Boolean =
    first > 0 || (heroBottom != null && heroBottom <= barBottom)
