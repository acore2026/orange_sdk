package com.rayneo.agent.sdk.transport

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.URI

/** Resolves the source address selected by the system route to the MASQUE peer. */
class RouteLocalAddressResolver : LocalAddressResolver {
    override fun resolve(serverUri: URI): String {
        val port = if (serverUri.port > 0) serverUri.port else 443
        val candidates = runCatching {
            java.net.InetAddress.getAllByName(serverUri.host)
                .sortedByDescending { it is Inet4Address }
        }.getOrElse { error ->
            throw AgentSdkException(
                ErrorCode.MASQUE_CONNECT_FAILED,
                "Cannot resolve MASQUE server for automatic source-address selection",
                "masqueServerUrl",
                cause = error,
            )
        }
        var lastError: Exception? = null
        candidates.forEach { remote ->
            try {
                DatagramSocket().use { socket ->
                    socket.connect(InetSocketAddress(remote, port))
                    val selected = socket.localAddress
                    val selectedText = selected.hostAddress
                    if (!selected.isAnyLocalAddress && !selectedText.isNullOrBlank()) {
                        return selectedText.substringBefore('%')
                    }
                }
            } catch (error: Exception) {
                lastError = error
            }
        }
        throw AgentSdkException(
            ErrorCode.MASQUE_CONNECT_FAILED,
            "No local address can reach the MASQUE server",
            "masqueServerUrl",
            cause = lastError,
        )
    }
}
