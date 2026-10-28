package dev.mnema.app.ui.recordings

import dev.mnema.app.model.DialogLine

private const val MIC = "mic"

fun isMine(line: DialogLine): Boolean = line.speaker == MIC

/** The first line of a run by one speaker: it gets an avatar shape. */
fun firstInRun(dialog: List<DialogLine>, index: Int): Boolean = index == 0 || dialog[index - 1].speaker != dialog[index].speaker
