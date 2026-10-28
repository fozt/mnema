package dev.mnema.app.helper

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class HelperProtocolTest {
    private val sourceDir = "/data/app/~~a==/dev.mnema.app-b==/base.apk"
    private val nonce = "0123456789abcdef0123456789abcdef"

    @Test
    fun launchCommandsAreExactlyTwoShellCalls() {
        val commands = HelperProtocol.launchCommands(sourceDir, 1700000000000, nonce)

        assertEquals(
            listOf(
                "pkill -f '[d]ev.mnema.app.helper.HelperMain'",
                "umask 077; CLASSPATH='$sourceDir' setsid nohup app_process / dev.mnema.app.helper.HelperMain " +
                    "'$sourceDir' 1700000000000 $nonce > /data/local/tmp/mnema-helper.log 2>&1 < /dev/null &",
            ),
            commands,
        )
    }

    @Test
    fun sourceDirWithQuoteIsRejected() {
        assertRejected { HelperProtocol.launchCommands("/data/app/x'; reboot; '/base.apk", 1, nonce) }
        for (bad in listOf("xyz", nonce.uppercase(), "$nonce\n", nonce.dropLast(1))) {
            assertRejected { HelperProtocol.launchCommands(sourceDir, 1, bad) }
        }
    }

    @Test
    fun newNonceIsThirtyTwoLowercaseHexAndDiffers() {
        val first = HelperProtocol.newNonce()
        val second = HelperProtocol.newNonce()

        assertTrue(first, Regex("^[0-9a-f]{32}$").matches(first))
        assertTrue(second, Regex("^[0-9a-f]{32}$").matches(second))
        assertNotEquals(first, second)
    }

    private fun assertRejected(block: () -> Unit) {
        try {
            block()
            fail("IllegalArgumentException expected")
        } catch (_: IllegalArgumentException) {
        }
    }
}
