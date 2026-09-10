package com.rayneo.agent.sdk.vpn

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Intent
import android.net.VpnService
import android.os.Binder
import android.os.IBinder
import android.os.ParcelFileDescriptor
import android.util.Log
import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.transport.TunnelConfiguration

class AgentVpnService : VpnService() {
    inner class LocalBinder : Binder() {
        val service: AgentVpnService get() = this@AgentVpnService
    }

    private val binder = LocalBinder()

    override fun onCreate() {
        super.onCreate()
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                "Agent network tunnel",
                NotificationManager.IMPORTANCE_LOW,
            )
        )
        val notification = android.app.Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentTitle("Agent network tunnel")
            .setContentText("MASQUE CONNECT-IP is active")
            .setOngoing(true)
            .build()
        startForeground(NOTIFICATION_ID, notification)
    }

    override fun onBind(intent: Intent?): IBinder? =
        if (intent?.action == SERVICE_INTERFACE) super.onBind(intent) else binder

    override fun onRevoke() {
        Log.e(TAG, "VPN permission revoked by Android; Agent TUN is no longer usable")
        super.onRevoke()
    }

    override fun onDestroy() {
        Log.w(TAG, "AgentVpnService destroyed")
        super.onDestroy()
    }

    @Synchronized
    fun establishTun(configuration: TunnelConfiguration): Int {
        val (address, prefix) = parseCidr(configuration.agentTunCidr)
        val builder = Builder()
            .setSession("Agent MASQUE")
            .setMtu(configuration.mtu)
            .addAddress(address, prefix)
        configuration.routes.sorted().forEach { route ->
            val (network, routePrefix) = parseCidr(route)
            builder.addRoute(network, routePrefix)
        }
        val descriptor = builder.establish() ?: throw AgentSdkException(
            ErrorCode.TUN_CREATE_FAILED,
            "VpnService.Builder.establish() returned null",
        )
        return descriptor.detachFd().also { fd ->
            Log.i(
                TAG,
                "TUN established fd=$fd address=${configuration.agentTunCidr} " +
                    "routes=${configuration.routes.sorted()} mtu=${configuration.mtu}",
            )
        }
    }

    fun protectQuicSocket(socketFd: Int): Boolean = protect(socketFd)

    private fun parseCidr(cidr: String): Pair<String, Int> {
        val separator = cidr.lastIndexOf('/')
        if (separator <= 0) {
            throw AgentSdkException(ErrorCode.INVALID_ARGUMENT, "Invalid CIDR: $cidr")
        }
        val address = cidr.substring(0, separator)
        val prefix = cidr.substring(separator + 1).toIntOrNull()
            ?: throw AgentSdkException(ErrorCode.INVALID_ARGUMENT, "Invalid CIDR: $cidr")
        return address to prefix
    }

    private companion object {
        const val TAG = "AgentSdkVpn"
        const val CHANNEL_ID = "agent_masque_tunnel"
        const val NOTIFICATION_ID = 41001
    }
}

class VpnTunnelController(
    private val service: AgentVpnService,
) : com.rayneo.agent.sdk.transport.TunnelController {
    private lateinit var baseConfiguration: TunnelConfiguration
    private val groupPeers = mutableMapOf<String, Set<String>>()
    private var fdSwapper: (suspend (Int) -> Unit)? = null
    private var tunReplacedListener: (suspend () -> Unit)? = null
    private var nativeOwnsTunFd = false
    override var tunFd: Int = -1
        private set
    override val clientIdentityDirectory: String
        get() = service.noBackupFilesDir.resolve("agent-sdk/tls").absolutePath

    override suspend fun establish(configuration: TunnelConfiguration) {
        baseConfiguration = configuration
        tunFd = service.establishTun(configuration)
        nativeOwnsTunFd = false
    }

    override suspend fun replaceGroupPeers(groupId: String, peerIps: Set<String>) {
        check(::baseConfiguration.isInitialized) { "Tunnel has not been established" }
        val previous = groupPeers[groupId]
        val previousRoutes = aggregateTunnelRoutes(baseConfiguration.routes, groupPeers)
        if (peerIps.isEmpty()) groupPeers.remove(groupId) else groupPeers[groupId] = peerIps
        val routes = aggregateTunnelRoutes(baseConfiguration.routes, groupPeers)
        if (routes == previousRoutes) {
            Log.i(
                TAG,
                "Skipping TUN route rebuild key=$groupId peers=${peerIps.sorted()}: " +
                    "aggregate routes unchanged=${routes.sorted()}",
            )
            return
        }
        Log.i(
            TAG,
            "Rebuilding TUN routes key=$groupId peers=${peerIps.sorted()} " +
                "all_routes=${routes.sorted()} old_fd=$tunFd",
        )
        val newFd = try {
            service.establishTun(baseConfiguration.copy(routes = routes.toSet()))
        } catch (error: Exception) {
            if (previous == null) groupPeers.remove(groupId) else groupPeers[groupId] = previous
            throw AgentSdkException(
                ErrorCode.ROUTE_CONFIG_FAILED,
                "Failed to rebuild VPN routes",
                cause = error,
            )
        }
        try {
            fdSwapper?.invoke(newFd)
                ?: throw AgentSdkException(
                    ErrorCode.ROUTE_CONFIG_FAILED,
                    "MASQUE TUN swapper is not registered",
                )
            tunFd = newFd
            nativeOwnsTunFd = true
            Log.i(TAG, "TUN route rebuild complete key=$groupId new_fd=$newFd")
        } catch (error: Exception) {
            ParcelFileDescriptor.adoptFd(newFd).close()
            if (previous == null) groupPeers.remove(groupId) else groupPeers[groupId] = previous
            throw error
        }
        try {
            tunReplacedListener?.invoke()
        } catch (error: Exception) {
            Log.e(
                TAG,
                "TUN fd swap committed but post-swap listener recovery failed key=$groupId",
                error,
            )
            throw error
        }
    }

    override fun currentAllowedPeerIps(): Set<String> =
        groupPeers.values.flatten().toSet() +
            baseConfiguration.routes.map { it.substringBefore('/') }

    override fun setTunFdSwapper(swapper: suspend (Int) -> Unit) {
        fdSwapper = swapper
        nativeOwnsTunFd = true
    }

    override fun setTunReplacedListener(listener: suspend () -> Unit) {
        tunReplacedListener = listener
    }

    override suspend fun close() {
        groupPeers.clear()
        fdSwapper = null
        tunReplacedListener = null
        if (tunFd >= 0 && !nativeOwnsTunFd) {
            ParcelFileDescriptor.adoptFd(tunFd).close()
        }
        tunFd = -1
        nativeOwnsTunFd = false
    }

    private companion object {
        const val TAG = "AgentSdkVpn"
    }
}

internal fun aggregateTunnelRoutes(
    baseRoutes: Set<String>,
    keyedPeers: Map<String, Set<String>>,
): Set<String> = baseRoutes + keyedPeers.values.flatten().map(::hostRoute)

private fun hostRoute(ip: String): String = "$ip/${if (ip.contains(':')) 128 else 32}"
