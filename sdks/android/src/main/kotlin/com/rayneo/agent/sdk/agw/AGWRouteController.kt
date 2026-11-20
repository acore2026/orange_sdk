package com.rayneo.agent.sdk.agw

import com.rayneo.agent.sdk.transport.TunnelController
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/** Serialize group, compute and AGW route changes only when the extension is enabled.
 * Reconnect fd replacement keeps the existing MASQUE lifecycle lock ordering. */
internal class AGWRouteController(private val delegate: TunnelController) : TunnelController by delegate {
    private val changes = Mutex()
    override suspend fun replaceGroupPeers(groupId: String, peerIps: Set<String>) = changes.withLock {
        delegate.replaceGroupPeers(groupId, peerIps)
    }
}
