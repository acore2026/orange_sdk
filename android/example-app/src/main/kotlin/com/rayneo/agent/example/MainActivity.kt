package com.rayneo.agent.example

import android.app.Activity
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.net.VpnService
import android.os.Bundle
import android.os.IBinder
import android.widget.TextView
import com.rayneo.agent.sdk.AgentSdk
import com.rayneo.agent.sdk.model.AgentProfile
import com.rayneo.agent.sdk.model.NetworkMessageAction
import com.rayneo.agent.sdk.model.NetworkMessageType
import com.rayneo.agent.sdk.security.DemoAcceptAllProofVerifier
import com.rayneo.agent.sdk.security.DemoMessageSignatureVerifier
import com.rayneo.agent.sdk.security.DemoMessageSigner
import com.rayneo.agent.sdk.transport.GroupMessageListener
import com.rayneo.agent.sdk.transport.NetworkMessageListener
import com.rayneo.agent.sdk.vpn.AgentVpnService
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.serialization.json.buildJsonObject

class MainActivity : Activity() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private lateinit var status: TextView
    private var vpnService: AgentVpnService? = null
    private var sdk: AgentSdk? = null

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            vpnService = (binder as AgentVpnService.LocalBinder).service
            requestVpnOrStart()
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            vpnService = null
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        status = TextView(this).apply { text = "Binding AgentVpnService…" }
        setContentView(status)
        bindService(
            Intent(this, AgentVpnService::class.java),
            connection,
            Context.BIND_AUTO_CREATE,
        )
    }

    private fun requestVpnOrStart() {
        val permission = VpnService.prepare(this)
        if (permission != null) {
            startActivityForResult(permission, VPN_PERMISSION_REQUEST)
        } else {
            startSdk()
        }
    }

    @Deprecated("Example uses the platform callback to avoid extra UI dependencies")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == VPN_PERMISSION_REQUEST && resultCode == RESULT_OK) startSdk()
    }

    private fun startSdk() {
        val service = vpnService ?: return
        val config = ExampleConfig.fromIntent(intent) ?: run {
            status.text = ExampleConfig.usage
            return
        }
        // Demo verifier is for this lab application only. Production must inject
        // the network trust-anchor-backed proof verifier.
        val value = AgentSdk.create(
            service,
            DemoAcceptAllProofVerifier(),
            DemoMessageSigner,
            DemoMessageSignatureVerifier,
        )
        value.restoreLocalProfile(
            AgentProfile(config.agentId, config.agentName, buildJsonObject { })
        )
        value.registerNetworkMessageListener(NetworkMessageListener { type, _ ->
            when (type) {
                NetworkMessageType.GROUP_INVITATION -> NetworkMessageAction.ACCEPT
                NetworkMessageType.GROUP_CONFIG -> NetworkMessageAction.ACK
                else -> NetworkMessageAction.REJECT
            }
        })
        value.registerGroupMessageListener(GroupMessageListener { groupId, sender, payload ->
            status.text = "A2A $groupId from $sender: $payload"
        })
        sdk = value
        scope.launch {
            runCatching {
                value.initialize(
                    agentRuntimeIp = config.runtimeIp,
                    agentRuntimePort = config.runtimePort,
                    localVlanIp = config.localVlanIp,
                    localTcpPort = config.tcpPort,
                    localUdpPort = config.udpPort,
                    masqueServerUrl = config.masqueUrl,
                    masqueAuthorization = config.masqueToken?.let { "Bearer $it" },
                )
            }.onSuccess { status.text = "READY ${it.agentTcpEndpoint}" }
                .onFailure { status.text = "FAILED: ${it.message}" }
        }
    }

    override fun onDestroy() {
        scope.launch { sdk?.close() }
        runCatching { unbindService(connection) }
        scope.cancel()
        super.onDestroy()
    }

    private data class ExampleConfig(
        val runtimeIp: String,
        val runtimePort: Int,
        val localVlanIp: String,
        val tcpPort: Int,
        val udpPort: Int,
        val masqueUrl: String,
        val agentId: String,
        val agentName: String,
        val masqueToken: String?,
    ) {
        companion object {
            fun fromIntent(intent: Intent): ExampleConfig? {
                val runtimeIp = intent.getStringExtra("runtime_ip") ?: return null
                val localVlanIp = intent.getStringExtra("local_vlan_ip") ?: return null
                val masqueUrl = intent.getStringExtra("masque_url") ?: return null
                val agentId = intent.getStringExtra("agent_id") ?: return null
                val agentName = intent.getStringExtra("agent_name") ?: return null
                return ExampleConfig(
                    runtimeIp,
                    intent.getIntExtra("runtime_port", 8080),
                    localVlanIp,
                    intent.getIntExtra("tcp_port", 4001),
                    intent.getIntExtra("udp_port", 28443),
                    masqueUrl,
                    agentId,
                    agentName,
                    intent.getStringExtra("masque_token"),
                )
            }

            val usage = """
                Pass deployment values through intent extras; no IP is hardcoded:
                runtime_ip, runtime_port, local_vlan_ip, tcp_port, udp_port,
                masque_url, agent_id, agent_name, and optional masque_token.
            """.trimIndent()
        }
    }

    private companion object {
        const val VPN_PERMISSION_REQUEST = 1001
    }
}
