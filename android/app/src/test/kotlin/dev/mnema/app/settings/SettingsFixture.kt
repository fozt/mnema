package dev.mnema.app.settings

import dev.mnema.app.model.Lang

/** Settings with one active node by connection string. */
internal fun settingsFor(connection: String, lang: Lang = Lang.AUTO): Settings =
    Settings(nodes = listOfNotNull(nodeOf(connection)), activeNode = nodeOf(connection)?.address.orEmpty(), lang = lang)
