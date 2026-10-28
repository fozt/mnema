package dev.mnema.app

import dev.mnema.app.settings.NodeEntry
import dev.mnema.app.settings.Settings
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class RedactTest {
    @After
    fun tearDown() = Redact.install { emptyList() }

    @Test
    fun installedSecretsAreHidden() {
        val token = "server-token-0123456789"
        val key = "my-model-key-xyz"
        Redact.install { listOf(token, key) }

        val out = Redact.redact("ошибка $key, токен $token")

        assertEquals("ошибка ***, токен ***", out)
    }

    @Test
    fun secretsAreReadOnEveryCall() {
        var secrets = listOf("first-secret-1")
        Redact.install { secrets }
        secrets = listOf("second-secret-2")

        assertEquals("first-secret-1 ***", Redact.redact("first-secret-1 second-secret-2"))
    }

    @Test
    fun shortSecretsAndPlainTextStay() {
        Redact.install { listOf("abc", "") }

        assertEquals("abc и ключ sk-short", Redact.redact("abc и ключ sk-short"))
        assertEquals("", Redact.redact(""))
    }

    @Test
    fun longerSecretGoesFirst() {
        Redact.install { listOf("secret-12", "secret-123456") }

        val out = Redact.redact("ключ secret-123456")

        assertEquals("ключ ***", out)
        assertFalse(out.contains("3456"))
    }
    @Test
    fun tokensOfAllSavedNodesAreHiddenIncludingAnUnselectedNode() {
        val settings = Settings(nodes = listOf(
            NodeEntry("http://box:8000#token=active-node-secret", "Сервер"),
            NodeEntry("http://mac:8000#token=other-node-secret", "Mac"),
        ), activeNode = "http://box:8000")
        Redact.install { secretsOf(settings) }
        assertEquals("*** ***", Redact.redact("active-node-secret other-node-secret"))
    }
}
