package com.rayneo.agent.sdk.masque

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.transport.MasqueConfiguration
import com.rayneo.agent.sdk.transport.MasqueTransport
import com.rayneo.agent.sdk.transport.MasqueTransportStatistics
import com.rayneo.agent.sdk.vpn.AgentVpnService
import androidx.annotation.Keep
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withContext
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import android.os.ParcelFileDescriptor

interface MasqueNativeCore {
    fun start(tunFd: Int, serverUrl: String, authorization: String?, localVlanIp: String,
              agentTunCidr: String, mtu: Int, identityDirectory: String): Long
    fun nativeReplaceTunFd(handle: Long, tunFd: Int): Boolean
    fun nativeStop(handle: Long)
    fun nativeIsAlive(handle: Long): Boolean
    fun statistics(handle: Long): MasqueTransportStatistics
}

class NativeMasqueBridge(
    private val vpnService: AgentVpnService,
) : MasqueNativeCore {
    companion object {
        private val libraryLoadError = runCatching {
            System.loadLibrary("masque_core")
        }.exceptionOrNull()
        private const val STAT_DOWNLINK_PACKETS = 1
        private const val STAT_DOWNLINK_PACKETS_OVER_TUN_MTU = 2
        private const val STAT_DOWNLINK_READ_BUFFER_TOO_SMALL = 3
        private const val STAT_UPLINK_DATAGRAM_TOO_LARGE = 4
        private const val STAT_MAX_DOWNLINK_PACKET_BYTES = 5
    }

    external fun nativeStart(
        tunFd: Int,
        serverUrl: String,
        authorization: String?,
        localVlanIp: String,
        agentTunCidr: String,
        mtu: Int,
        identityDirectory: String,
    ): Long

    external override fun nativeReplaceTunFd(handle: Long, tunFd: Int): Boolean
    external fun nativeGetStat(handle: Long, statistic: Int): Long
    external override fun nativeStop(handle: Long)
    external override fun nativeIsAlive(handle: Long): Boolean

    override fun start(
        tunFd: Int,
        serverUrl: String,
        authorization: String?,
        localVlanIp: String,
        agentTunCidr: String,
        mtu: Int,
        identityDirectory: String,
    ): Long {
        libraryLoadError?.let { error ->
            throw AgentSdkException(
                ErrorCode.MASQUE_CONNECT_FAILED,
                "libmasque_core.so is not packaged for this Android ABI",
                cause = error,
            )
        }
        return nativeStart(
            tunFd,
            serverUrl,
            authorization,
            localVlanIp,
            agentTunCidr,
            mtu,
            identityDirectory,
        )
    }

    @Keep
    fun protectQuicSocket(socketFd: Int): Boolean = vpnService.protectQuicSocket(socketFd)

    override fun statistics(handle: Long): MasqueTransportStatistics = MasqueTransportStatistics(
        downlinkPackets = nativeGetStat(handle, STAT_DOWNLINK_PACKETS),
        downlinkPacketsOverTunMtu = nativeGetStat(handle, STAT_DOWNLINK_PACKETS_OVER_TUN_MTU),
        downlinkReadBufferTooSmall = nativeGetStat(handle, STAT_DOWNLINK_READ_BUFFER_TOO_SMALL),
        uplinkDatagramTooLarge = nativeGetStat(handle, STAT_UPLINK_DATAGRAM_TOO_LARGE),
        maxDownlinkPacketBytes = nativeGetStat(handle, STAT_MAX_DOWNLINK_PACKET_BYTES),
    )

}

class NativeMasqueTransport(
    private val bridge: MasqueNativeCore,
    private val reconnectScope: CoroutineScope = CoroutineScope(Dispatchers.IO),
    private val ioDispatcher: kotlinx.coroutines.CoroutineDispatcher = Dispatchers.IO,
    private val closePendingFd: (Int) -> Unit = { ParcelFileDescriptor.adoptFd(it).close() },
) : MasqueTransport {
    @Volatile
    private var handle: Long = 0
    @Volatile
    private var lastStatistics = MasqueTransportStatistics()
    private var reconnectTun: (suspend () -> Int)? = null
    private var reconnectSourceAddress: (suspend () -> String)? = null
    private var reconnectJob: Job? = null
    private var onReconnected: suspend () -> Unit = {}
    private val lifecycle = Mutex()
    @Volatile
    override var connected: Boolean = false
        private set

    override suspend fun start(tunFd: Int, configuration: MasqueConfiguration) = lifecycle.withLock {
        check(handle == 0L) { "MASQUE transport is already started" }
        startNative(tunFd, configuration, ownsPendingFd = false)
        reconnectJob = reconnectScope.launch {
            var retryDelay = 1_000L
            while (isActive) {
                delay(if (connected) 5_000L else retryDelay)
                try {
                    lifecycle.withLock {
                        if (handle != 0L && bridge.nativeIsAlive(handle) && connected) return@withLock
                        connected = false
                        val factory = reconnectTun ?: return@withLock
                        if (handle != 0L) {
                            bridge.nativeStop(handle)
                            handle = 0L
                        }
                        val newFd = factory()
                        val currentConfiguration = configuration.copy(
                            localVlanIp = reconnectSourceAddress?.invoke() ?: configuration.localVlanIp,
                        )
                        startNative(newFd, currentConfiguration, ownsPendingFd = true)
                        try {
                            onReconnected()
                        } catch (error: Exception) {
                            connected = false
                            throw error
                        }
                        retryDelay = 1_000L
                        android.util.Log.i("AgentSdkVpn", "MASQUE reconnect succeeded; local listeners restored")
                    }
                } catch (error: CancellationException) {
                    throw error
                } catch (error: Exception) {
                    connected = false
                    android.util.Log.w("AgentSdkVpn", "MASQUE reconnect failed; retry in ${retryDelay}ms", error)
                    retryDelay = (retryDelay * 2).coerceAtMost(15_000L)
                }
            }
        }
    }

    private suspend fun startNative(tunFd: Int, configuration: MasqueConfiguration, ownsPendingFd: Boolean) {
        lastStatistics = MasqueTransportStatistics()
        var startedHandle = 0L
        try {
            withContext(ioDispatcher) {
                startedHandle = bridge.start(
                    tunFd = tunFd,
                    serverUrl = configuration.serverUrl,
                    authorization = configuration.authorization,
                    localVlanIp = configuration.localVlanIp,
                    agentTunCidr = configuration.agentTunCidr,
                    mtu = configuration.mtu,
                    identityDirectory = configuration.identityDirectory,
                )
            }
        } catch (error: CancellationException) {
            // JNI cannot be interrupted while the native QUIC handshake is in progress. If it
            // completed just as the caller was cancelled, close the returned handle explicitly
            // instead of leaking a live tunnel whose result can no longer be delivered.
            if (startedHandle != 0L) {
                withContext(NonCancellable + ioDispatcher) {
                    bridge.nativeStop(startedHandle)
                }
            }
            throw error
        } finally {
            if (ownsPendingFd && startedHandle == 0L) {
                closePendingFd(tunFd)
            }
        }
        if (startedHandle == 0L) {
            throw AgentSdkException(
                ErrorCode.MASQUE_CONNECT_FAILED,
                "Native CONNECT-IP core failed to start",
            )
        }
        handle = startedHandle
        connected = true
    }

    override fun setReconnectTun(factory: suspend () -> Int, onConnected: suspend () -> Unit) {
        reconnectTun = factory
        onReconnected = onConnected
    }

    override fun setReconnectSourceAddress(factory: suspend () -> String) {
        reconnectSourceAddress = factory
    }

    override suspend fun replaceTunFd(tunFd: Int) = lifecycle.withLock {
        val currentHandle = handle
        val replaced = currentHandle != 0L && withContext(NonCancellable + ioDispatcher) {
            bridge.nativeReplaceTunFd(currentHandle, tunFd)
        }
        if (!replaced) {
            throw AgentSdkException(
                ErrorCode.ROUTE_CONFIG_FAILED,
                "Native CONNECT-IP core rejected the replacement TUN fd",
            )
        }
    }

    override fun statistics(): MasqueTransportStatistics {
        val currentHandle = handle
        if (currentHandle == 0L) return lastStatistics
        return bridge.statistics(currentHandle).also { lastStatistics = it }
    }

    override suspend fun close() {
        reconnectJob?.cancel()
        lifecycle.withLock {
            reconnectJob?.cancel(); reconnectJob = null
            val currentHandle = handle
            if (currentHandle != 0L) {
                lastStatistics = bridge.statistics(currentHandle)
            }
            handle = 0
            connected = false
            if (currentHandle != 0L) {
                withContext(NonCancellable + ioDispatcher) {
                    bridge.nativeStop(currentHandle)
                }
            }
        }
    }
}
