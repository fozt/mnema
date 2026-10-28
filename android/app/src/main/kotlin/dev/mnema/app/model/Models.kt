package dev.mnema.app.model

import dev.mnema.app.i18n.tr

data class Summary(
    val about: String = "",
    val points: List<String> = emptyList(),
    val pointTimes: List<Double?> = List(points.size) { null },
)

/** [speaker]: "mic", "system", or empty when the paragraph has no track label. */
data class DialogLine(val speaker: String, val text: String, val at: Double? = null)

/** Automatic recognition: the node picks the engine; other ids come from the node's list. */
const val AUTO_ASR = "auto"

enum class Lang(val code: String?) {
    AUTO(null),
    RU("ru"),
    EN("en");

    val title: String get() = when (this) {
        AUTO -> tr("Automatic")
        RU -> tr("Russian")
        EN -> tr("English")
    }
}
