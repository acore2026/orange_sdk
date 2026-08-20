package com.rayneo.agent.sdk.masque

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.transport.MasqueConfiguration
import com.rayneo.agent.sdk.transport.MasqueTransport
import com.rayneo.agent.sdk.vpn.AgentVpnService
import androidx.annotation.Keep

class NativeMasqueBridge(
    private val vpnService: AgentVpnService,
) {
    companion object {
        private val libraryLoadError = runCatching {
            System.loadLibrary("masque_core")
        }.exceptionOrNull()
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

    external fun nativeReplaceTunFd(handle: Long, tunFd: Int): Boolean
    external fun nativeStop(handle: Long)

    fun start(
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
}

class NativeMasqueTransport(
    private val bridge: NativeMasqueBridge,
) : MasqueTransport {
    private var handle: Long = 0
    override var connected: Boolean = false
        private set

    override suspend fun start(tunFd: Int, configuration: MasqueConfiguration) {
        handle = bridge.start(
            tunFd = tunFd,
            serverUrl = configuration.serverUrl,
            authorization = configuration.authorization,
            localVlanIp = configuration.localVlanIp,
            agentTunCidr = configuration.agentTunCidr,
            mtu = configuration.mtu,
            identityDirectory = configuration.identityDirectory,
        )
        if (handle == 0L) {
            throw AgentSdkException(
                ErrorCode.MASQUE_CONNECT_FAILED,
                "Native CONNECT-IP core failed to start",
            )
        }
        connected = true
    }

    override suspend fun replaceTunFd(tunFd: Int) {
        if (handle == 0L || !bridge.nativeReplaceTunFd(handle, tunFd)) {
            throw AgentSdkException(
                ErrorCode.ROUTE_CONFIG_FAILED,
                "Native CONNECT-IP core rejected the replacement TUN fd",
            )
        }
    }

    override suspend fun close() {
        if (handle != 0L) bridge.nativeStop(handle)
        handle = 0
        connected = false
    }
}
