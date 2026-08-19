package com.rayneo.agent.sdk

import com.rayneo.agent.sdk.model.AgentProfile
import com.rayneo.agent.sdk.model.NetworkMessageAction
import com.rayneo.agent.sdk.transport.LocalServer
import com.rayneo.agent.sdk.transport.MediaOffloadAdapter
import com.rayneo.agent.sdk.transport.MessageSigner
import com.rayneo.agent.sdk.transport.MasqueConfiguration
import com.rayneo.agent.sdk.transport.MasqueTransport
import com.rayneo.agent.sdk.transport.NetworkMessageListener
import com.rayneo.agent.sdk.transport.PeerMessenger
import com.rayneo.agent.sdk.transport.ProofVerifier
import com.rayneo.agent.sdk.transport.RuntimeTransport
import com.rayneo.agent.sdk.transport.TunnelConfiguration
import com.rayneo.agent.sdk.transport.TunnelController
import com.rayneo.agent.sdk.transport.VideoTrack
import com.rayneo.agent.sdk.transport.VideoUploadHandle
import com.rayneo.agent.sdk.model.OffloadingSession
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.time.Instant

class AgentSdkGroupConfigTest {
    private lateinit var tunnel: FakeTunnel
    private lateinit var masque: FakeMasque
    private lateinit var runtime: FakeRuntime
    private lateinit var server: FakeServer
    private lateinit var peer: FakePeer
    private lateinit var media: FakeMedia
    private lateinit var sdk: AgentSdk

    @Before
    fun setUp() {
        tunnel = FakeTunnel()
        masque = FakeMasque()
        runtime = FakeRuntime()
        server = FakeServer()
        peer = FakePeer()
        media = FakeMedia()
        sdk = AgentSdk(
            tunnelController = tunnel,
            masqueTransport = masque,
            proofVerifier = ProofVerifier { },
            messageSigner = FakeMessageSigner,
            peerMessenger = peer,
            runtimeFactory = { _, _ -> runtime },
            localServerFactory = { server },
            mediaOffloadAdapter = media,
        )
    }

    @Test
    fun `group config caches by agent id and installs peer route`() = runTest {
        initializeSdk()
        sdk.registerNetworkMessageListener(NetworkMessageListener { _, _ ->
            NetworkMessageAction.ACK
        })

        val action = server.onGroupConfig!!(groupConfig())

        assertEquals(NetworkMessageAction.ACK, action)
        val snapshot = sdk.getGroupSnapshot("g1")
        assertNotNull(snapshot)
        val target = snapshot!!.membersByAgentId[PEER_ID]!!
        assertEquals("8.8.8.8", target.agentIp)
        assertEquals(4001, target.tcpPort)
        assertEquals(28443, target.udpPort)
        assertEquals(setOf("8.8.8.8"), tunnel.groupPeers["g1"])
    }

    @Test
    fun `send message uses only cached IP and TCP port`() = runTest {
        initializeSdk()
        sdk.registerNetworkMessageListener(NetworkMessageListener { _, _ ->
            NetworkMessageAction.ACK
        })
        server.onGroupConfig!!(groupConfig(peerPort = "4567"))

        val receipt = sdk.sendMessage(
            "g1",
            PEER_ID,
            buildJsonObject { put("command", "patrol") },
        )

        assertTrue(receipt.delivered)
        assertEquals("8.8.8.8", peer.ip)
        assertEquals(4567, peer.port)
        assertEquals(PEER_ID, peer.body!!["target_agent_id"].toString().trim('"'))
    }

    @Test
    fun `listener reject leaves cache and routes unchanged`() = runTest {
        initializeSdk()
        sdk.registerNetworkMessageListener(NetworkMessageListener { _, _ ->
            NetworkMessageAction.REJECT
        })

        val action = server.onGroupConfig!!(groupConfig())

        assertEquals(NetworkMessageAction.REJECT, action)
        assertEquals(null, sdk.getGroupSnapshot("g1"))
        assertTrue(tunnel.groupPeers.isEmpty())
    }

    @Test
    fun `port must be a decimal string in range`() = runTest {
        initializeSdk()
        sdk.registerNetworkMessageListener(NetworkMessageListener { _, _ ->
            NetworkMessageAction.ACK
        })

        val error = runCatching {
            server.onGroupConfig!!(groupConfig(peerPort = "65536"))
        }.exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.GROUP_CONFIG_INVALID, error.code)
        assertEquals(null, sdk.getGroupSnapshot("g1"))
    }

    @Test
    fun `version must use semantic version syntax`() = runTest {
        initializeSdk()
        sdk.registerNetworkMessageListener(NetworkMessageListener { _, _ ->
            NetworkMessageAction.ACK
        })
        val invalid = buildJsonObject {
            groupConfig().forEach { (key, value) ->
                put(key, if (key == "version") JsonPrimitive("1") else value)
            }
        }

        val error = runCatching { server.onGroupConfig!!(invalid) }
            .exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.GROUP_CONFIG_INVALID, error.code)
    }

    @Test
    fun `send without committed config never falls back`() = runTest {
        initializeSdk()

        val error = runCatching {
            sdk.sendMessage("g1", PEER_ID, buildJsonObject { put("hello", "world") })
        }.exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.GROUP_NOT_ACTIVE, error.code)
        assertFalse(peer.called)
    }

    @Test
    fun `offloading video delegates to configured media adapter`() = runTest {
        initializeSdk()

        val session = sdk.createOffloadingSession(
            LOCAL_ID,
            "video_rendering",
            sandboxId = "sandbox-edge-1",
        )
        val upload = sdk.startVideoUpload(
            session.sessionId,
            cameraId = "2",
            width = 1280,
            height = 720,
            fps = 30,
            bitrateKbps = 2500,
        )
        val track = sdk.getProcessedVideoStream(session.sessionId)

        assertEquals("CONNECTED", session.state)
        assertEquals("session-1", media.connectedSession)
        assertEquals("camera-track-1", upload.trackId)
        assertEquals("processed-track-1", track.trackId)
        assertEquals("2", media.cameraId)
    }

    @Test
    fun `capability update uses dedicated POST endpoint`() = runTest {
        initializeSdk()
        val updates = listOf(buildJsonObject {
            put("update_type", "add_skill")
            put("skill_name", "camera")
            put("reference_vc_id", "vc-camera-002")
        })
        val credentials = listOf(buildJsonObject { put("id", "vc-camera-002") })

        sdk.updateCapabilities(LOCAL_ID, updates, credentials)

        assertEquals("POST", runtime.lastMethod)
        assertEquals("/arf/v1/agent-cards-update", runtime.lastPath)
        assertEquals(LOCAL_ID, runtime.lastBody!!["agent_id"].toString().trim('"'))
    }

    private suspend fun initializeSdk() {
        val result = sdk.initialize(
            agentRuntimeIp = "192.168.3.10",
            agentRuntimePort = 8080,
            localVlanIp = "192.168.1.10",
            localTcpPort = 4001,
            localUdpPort = 28443,
            agentTunCidr = "8.8.8.7/24",
            masqueServerUrl = "https://192.168.3.10:4433",
        )
        assertEquals("8.8.8.7:4001", result.agentTcpEndpoint)
        sdk.restoreLocalProfile(
            AgentProfile(LOCAL_ID, "Agent A", buildJsonObject { put("id", "vc-a") })
        )
    }

    private fun groupConfig(peerPort: String = "4001"): JsonObject = buildJsonObject {
        put("notification_type", "acf_group_config")
        put("version", "1.0.0")
        put("timestamp", Instant.now().toString())
        put("group_id", "g1")
        put("members", buildJsonObject {
            put("agent1", member(LOCAL_ID, "Agent A", "8.8.8.7", "4001", "did:key:a"))
            put("not-an-id", member(PEER_ID, "Agent B", "8.8.8.8", peerPort, "did:key:b"))
        })
        put("proof", buildJsonObject { put("jws", "test") })
    }

    private fun member(
        id: String,
        name: String,
        ip: String,
        tcpPort: String,
        key: String,
    ): JsonObject = buildJsonObject {
        put("agent_id", id)
        put("agent_name", name)
        put("capabilities", buildJsonArray { add(JsonPrimitive("text")) })
        put("agent_ip", ip)
        put("tcp_port", tcpPort)
        put("udp_port", "28443")
        put("did_key", key)
    }

    private class FakeTunnel : TunnelController {
        override val tunFd: Int = 42
        val groupPeers = mutableMapOf<String, Set<String>>()
        private var swapper: (suspend (Int) -> Unit)? = null

        override suspend fun establish(configuration: TunnelConfiguration) = Unit
        override suspend fun replaceGroupPeers(groupId: String, peerIps: Set<String>) {
            if (peerIps.isEmpty()) groupPeers.remove(groupId) else groupPeers[groupId] = peerIps
        }
        override fun currentAllowedPeerIps(): Set<String> = groupPeers.values.flatten().toSet()
        override fun setTunFdSwapper(swapper: suspend (Int) -> Unit) { this.swapper = swapper }
        override suspend fun close() = Unit
    }

    private class FakeMasque : MasqueTransport {
        override var connected: Boolean = false
        override suspend fun start(tunFd: Int, configuration: MasqueConfiguration) { connected = true }
        override suspend fun replaceTunFd(tunFd: Int) = Unit
        override suspend fun close() { connected = false }
    }

    private class FakeRuntime : RuntimeTransport {
        var lastMethod = ""
        var lastPath = ""
        var lastBody: JsonObject? = null

        override suspend fun connect() = Unit
        override suspend fun registerEndpoint(localIp: String, tcpPort: Int, udpPort: Int) = "reg-1"
        override suspend fun request(method: String, path: String, body: JsonObject): JsonObject {
            lastMethod = method
            lastPath = path
            lastBody = body
            return if (path == "/compute/v1/offloading-sessions") {
                buildJsonObject {
                    put("session_id", "session-1")
                    put("sandbox_id", "sandbox-edge-1")
                    put("state", "CONNECTING")
                    put("sdp_answer", "test-answer")
                    put("expires_at", "2026-08-18T12:00:00Z")
                }
            } else {
                buildJsonObject { }
            }
        }
        override suspend fun close() = Unit
    }

    private class FakeServer : LocalServer {
        var onGroupConfig: (suspend (JsonObject) -> NetworkMessageAction)? = null
        override suspend fun start(
            physicalIp: String,
            agentIp: String,
            tcpPort: Int,
            udpPort: Int,
            onGroupConfig: suspend (JsonObject) -> NetworkMessageAction,
            onGroupInvitation: suspend (JsonObject) -> NetworkMessageAction,
            onA2aMessage: suspend (JsonObject) -> Unit,
        ) { this.onGroupConfig = onGroupConfig }
        override suspend fun close() = Unit
    }

    private class FakePeer : PeerMessenger {
        var called = false
        var ip = ""
        var port = 0
        var body: JsonObject? = null
        override suspend fun send(
            ip: String,
            port: Int,
            body: JsonObject,
            timeoutMillis: Long,
        ): JsonObject {
            called = true
            this.ip = ip
            this.port = port
            this.body = body
            return buildJsonObject { put("ack", true) }
        }
    }

    private object FakeMessageSigner : MessageSigner {
        override suspend fun signA2a(payload: JsonObject): JsonObject = buildJsonObject {
            put("jws", "test-message-signature")
        }
    }

    private class FakeMedia : MediaOffloadAdapter {
        var connectedSession = ""
        var cameraId = ""

        override suspend fun connect(
            session: OffloadingSession,
            signaling: JsonObject,
            timeoutSeconds: Double,
        ) {
            assertEquals("test-answer", signaling["sdp_answer"].toString().trim('"'))
            connectedSession = session.sessionId
        }

        override suspend fun startVideoUpload(
            session: OffloadingSession,
            cameraId: String,
            width: Int,
            height: Int,
            fps: Int,
            bitrateKbps: Int,
        ): VideoUploadHandle {
            this.cameraId = cameraId
            return object : VideoUploadHandle {
                override val trackId = "camera-track-1"
                override var state = "RUNNING"
                override suspend fun pause() { state = "PAUSED" }
                override suspend fun resume() { state = "RUNNING" }
                override suspend fun stop() { state = "STOPPED" }
            }
        }

        override suspend fun getProcessedVideoTrack(
            session: OffloadingSession,
            timeoutSeconds: Double,
        ): VideoTrack = object : VideoTrack {
            override val trackId = "processed-track-1"
            override fun addSink(sink: Any) = Unit
            override fun removeSink(sink: Any) = Unit
        }

        override suspend fun close() = Unit
    }

    private companion object {
        const val LOCAL_ID = "did:example:agent-a"
        const val PEER_ID = "did:example:agent-b"
    }
}
