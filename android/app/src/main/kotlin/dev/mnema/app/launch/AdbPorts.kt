package dev.mnema.app.launch

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.firstOrNull
import kotlinx.coroutines.withTimeoutOrNull
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.ServerSocket
import java.util.concurrent.Executor

/** The debugging service is ours if one of its addresses is an interface address of this phone. */
fun acceptService(serviceHosts: List<InetAddress>, own: List<InetAddress>): Boolean =
    serviceHosts.any { host -> !host.isLoopbackAddress && own.any { it.address.contentEquals(host.address) } }

internal const val LOG_TAG = "MnemaLaunch"

object AdbPorts {
    const val HOST = "127.0.0.1"
    const val PAIRING = "_adb-tls-pairing._tcp"
    const val CONNECT = "_adb-tls-connect._tcp"
    private const val WIFI_WAIT_MS = 1000L

    suspend fun find(context: Context, type: String, timeoutMs: Long): Int? =
        withTimeoutOrNull(timeoutMs) { ports(context, type).firstOrNull() }

    fun ports(context: Context, type: String): Flow<Int> = callbackFlow {
        val nsd = context.getSystemService(NsdManager::class.java)
        val resolvers = mutableListOf<NsdManager.ServiceInfoCallback>()
        var closed = false
        val direct = Executor { it.run() }
        val discovery = object : NsdManager.DiscoveryListener {
            override fun onServiceFound(service: NsdServiceInfo) {
                val resolver = object : NsdManager.ServiceInfoCallback {
                    override fun onServiceUpdated(info: NsdServiceInfo) {
                        when {
                            info.port <= 0 -> Unit
                            !acceptService(info.hostAddresses, ownAddresses()) ->
                                Log.i(LOG_TAG, "$type: addresses ${info.hostAddresses} are not this phone's")
                            !listening(info.port) -> Log.i(LOG_TAG, "$type: port ${info.port} is not listening")
                            else -> trySend(info.port)
                        }
                    }

                    override fun onServiceInfoCallbackRegistrationFailed(errorCode: Int) {
                        Log.w(LOG_TAG, "$type: service address not resolved, code $errorCode")
                    }

                    override fun onServiceLost() = Unit
                    override fun onServiceInfoCallbackUnregistered() = Unit
                }
                synchronized(resolvers) {
                    if (closed) return
                    runCatching { nsd.registerServiceInfoCallback(service, direct, resolver) }
                        .onSuccess { resolvers += resolver }
                        .onFailure { Log.w(LOG_TAG, "$type: service address not requested", it) }
                }
            }

            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                Log.w(LOG_TAG, "$serviceType: discovery did not start, code $errorCode")
                channel.close()
            }

            override fun onDiscoveryStarted(serviceType: String) = Unit
            override fun onDiscoveryStopped(serviceType: String) = Unit
            override fun onServiceLost(service: NsdServiceInfo) = Unit
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) = Unit
        }
        try {
            nsd.discoverServices(type, NsdManager.PROTOCOL_DNS_SD, discovery)
        } catch (e: RuntimeException) {
            Log.w(LOG_TAG, "$type: discovery did not start", e)
            channel.close()
        }
        awaitClose {
            synchronized(resolvers) {
                closed = true
                resolvers.forEach { runCatching { nsd.unregisterServiceInfoCallback(it) } }
            }
            runCatching { nsd.stopServiceDiscovery(discovery) }
        }
    }

    suspend fun wifiConnected(context: Context): Boolean {
        val connectivity = context.getSystemService(ConnectivityManager::class.java)
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()
        val found = CompletableDeferred<Unit>()
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                found.complete(Unit)
            }
        }
        try {
            connectivity.registerNetworkCallback(request, callback)
        } catch (e: RuntimeException) {
            Log.w(LOG_TAG, "Wi-Fi check failed", e)
            return true
        }
        return try {
            withTimeoutOrNull(WIFI_WAIT_MS) { found.await() } != null
        } finally {
            runCatching { connectivity.unregisterNetworkCallback(callback) }
        }
    }

    private fun ownAddresses(): List<InetAddress> = try {
        NetworkInterface.getNetworkInterfaces()?.toList().orEmpty().flatMap { it.inetAddresses.toList() }
    } catch (_: IOException) {
        emptyList()
    }

    // As in Shizuku: the port is taken on 127.0.0.1 — so adbd listens on it
    private fun listening(port: Int): Boolean = try {
        ServerSocket().use { it.bind(InetSocketAddress(HOST, port), 1) }
        false
    } catch (_: IOException) {
        true
    }
}
