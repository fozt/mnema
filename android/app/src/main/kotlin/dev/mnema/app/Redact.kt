package dev.mnema.app

/** Device tokens stay out of remarks, meta, notifications and the log; the server hides model keys. */
object Redact {
    private const val MASK = "***"
    private const val SHORTEST_SECRET = 8

    @Volatile
    private var secrets: () -> List<String> = { emptyList() }

    /** [secrets] is read on every [redact]: tokens from settings are always fresh. */
    fun install(secrets: () -> List<String>) {
        this.secrets = secrets
    }

    fun redact(text: String): String {
        if (text.isEmpty()) return text
        val known = runCatching { secrets() }.getOrDefault(emptyList())
            .map { it.trim() }
            .filter { it.length >= SHORTEST_SECRET }
            .distinct()
            .sortedByDescending { it.length }
        var out = text
        for (secret in known) out = out.replace(secret, MASK)
        return out
    }
}
