package dev.mnema.app.launch

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.Inet6Address
import java.net.InetAddress

class AdbPortsTest {
    private val own = listOf(ip("192.168.1.5"), ip("fe80::1"))

    @Test
    fun serviceOnOwnInterfaceIsAccepted() {
        val scoped = Inet6Address.getByAddress(null, ip("fe80::1").address, 3)

        assertTrue(acceptService(listOf(ip("192.168.1.5")), own))
        assertTrue(acceptService(listOf(ip("10.0.0.9"), ip("fe80::1")), own))
        assertTrue("address with a scope id", acceptService(listOf(scoped), own))
    }

    @Test
    fun serviceOfAnotherDeviceIsRejected() {
        assertFalse(acceptService(listOf(ip("192.168.1.77")), own))
        assertFalse(acceptService(listOf(ip("fe80::2")), own))
        assertFalse(acceptService(emptyList(), own))
        assertFalse(acceptService(listOf(ip("192.168.1.5")), emptyList()))
    }

    @Test
    fun loopbackIsNeverProofOfOwnService() {
        val withLoopback = own + ip("127.0.0.1") + ip("::1")

        assertFalse(acceptService(listOf(ip("127.0.0.1")), withLoopback))
        assertFalse(acceptService(listOf(ip("::1")), withLoopback))
    }

    private fun ip(literal: String): InetAddress = InetAddress.getByName(literal)
}
