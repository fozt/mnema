package dev.mnema.app.i18n

/**
 * Russian catalog: English source string → Russian text, or a list of three plural forms.
 * Each area keeps its own map; a key shared by two maps must have the same value.
 */
internal val RU: Map<String, Any> = RU_UI + RU_DATA + RU_SYSTEM
