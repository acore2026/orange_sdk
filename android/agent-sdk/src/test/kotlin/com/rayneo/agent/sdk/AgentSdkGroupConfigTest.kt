package com.rayneo.agent.sdk

import com.rayneo.agent.sdk.model.AgentProfile
import com.rayneo.agent.sdk.model.NetworkMessageAction
import com.rayneo.agent.sdk.model.NetworkMessageType
import com.rayneo.agent.sdk.transport.LocalServer
import com.rayneo.agent.sdk.transport.ControlRequestAuthenticator
import com.rayneo.agent.sdk.transport.DevicePublicKeyProvider
import com.rayneo.agent.sdk.transport.GroupMessageListener
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
import com.rayneo.agent.sdk.security.TestCapabilityVcIssuer
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.time.Instant
import java.nio.file.Files
import java.security.KeyPairGenerator
import java.security.spec.ECGenParameterSpec
import java.util.Base64
import java.util.UUID

class AgentSdkGroupConfigTest {
    private lateinit var tunnel: FakeTunnel
    private lateinit var masque: FakeMasque
    private lateinit var runtime: FakeRuntime
    private lateinit var server: FakeServer
    private lateinit var peer: FakePeer
    private lateinit var media: FakeMedia
    private lateinit var testCapabilityIssuer: TestCapabilityVcIssuer
    private lateinit var sdk: AgentSdk

    @Before
    fun setUp() {
        tunnel = FakeTunnel()
        masque = FakeMasque()
        runtime = FakeRuntime()
        server = FakeServer()
        peer = FakePeer()
        media = FakeMedia()
        testCapabilityIssuer = TestCapabilityVcIssuer(
            Files.createTempDirectory("agent-sdk-test-capability-")
                .resolve("issuer-private-key.pem")
                .toFile()
        )
        sdk = AgentSdk(
            tunnelController = tunnel,
            masqueTransport = masque,
            proofVerifier = ProofVerifier { },
            controlRequestAuthenticator = FakeControlAuthenticator,
            devicePublicKeyProvider = FakeDevicePublicKeyProvider,
            messageSigner = FakeMessageSigner,
            peerMessenger = peer,
            runtimeFactory = { _, _ -> runtime },
            localServerFactory = { server },
            mediaOffloadAdapter = media,
            testCapabilityVcIssuer = testCapabilityIssuer,
        )
        sdk.importTestCapabilityIssuerPrivateKey(testPrivateKeyPem())
    }

    @Test
    fun `group config caches by agent id and installs peer route`() = runTest {
        initializeSdk()
        sdk.registerNetworkMessageListener(NetworkMessageListener { _, _ ->
            NetworkMessageAction.ACK
        })

        val action = runtime.deliverGroupConfig(groupConfig())

        assertEquals(NetworkMessageAction.ACK, action)
        val snapshot = sdk.getGroupSnapshot("g1")
        assertNotNull(snapshot)
        val target = snapshot!!.membersByAgentId[PEER_ID]!!
        assertEquals("8.8.8.8", target.agentIp)
        assertEquals(4001, target.tcpPort)
        assertEquals(0, target.udpPort)
        assertEquals(setOf("8.8.8.8"), tunnel.groupPeers["g1"])
    }

    @Test
    fun `initialize queries UE info and registers runtime downlink websocket`() = runTest {
        initializeSdk()

        assertEquals(1, runtime.ueInfoRequests)
        assertEquals("", runtime.lastPath)
        assertNotNull(runtime.downlinkHandler)
        assertEquals("8.8.8.7/32", tunnel.establishedConfiguration?.agentTunCidr)
    }

    @Test
    fun `NAS invitation is delivered through runtime websocket handler`() = runTest {
        initializeSdk()
        var receivedType: NetworkMessageType? = null
        sdk.registerNetworkMessageListener(NetworkMessageListener { messageType, _ ->
            receivedType = messageType
            NetworkMessageAction.ACCEPT
        })

        val action = runtime.deliverDownlink(
            "ACN_AGENT_GROUPING_INVITATION",
            buildJsonObject {
                put("group_config", buildJsonObject { put("group_name", "task-patrol") })
                put("group_administrator", buildJsonObject { put("agent_id", "a1") })
            },
        )

        assertEquals(NetworkMessageAction.ACCEPT, action)
        assertEquals(NetworkMessageType.GROUP_INVITATION, receivedType)
    }

    @Test
    fun `group config commits without a network listener`() = runTest {
        initializeSdk()

        val action = runtime.deliverGroupConfig(groupConfig())

        assertEquals(NetworkMessageAction.ACK, action)
        assertNotNull(sdk.getGroupSnapshot("g1"))
        assertEquals(setOf("8.8.8.8"), tunnel.groupPeers["g1"])
    }

    @Test
    fun `send message uses only cached IP and TCP port`() = runTest {
        initializeSdk()
        sdk.registerNetworkMessageListener(NetworkMessageListener { _, _ ->
            NetworkMessageAction.ACK
        })
        runtime.deliverGroupConfig(groupConfig(peerPort = "4567"))

        val receipt = sdk.sendMessage(
            "g1",
            PEER_ID,
            buildJsonObject { put("command", "patrol") },
            messageType = "control",
            taskId = "task-patrol",
        )

        assertTrue(receipt.delivered)
        assertEquals("agent.example", peer.ip)
        assertEquals(4567, peer.port)
        assertEquals(PEER_ID, peer.body!!["dst_agent_id"].toString().trim('"'))
        assertEquals(LOCAL_ID, peer.body!!["src_agent_id"].toString().trim('"'))
        assertEquals("control", peer.body!!["type"].toString().trim('"'))
        assertEquals("task-patrol", peer.body!!["task_id"].toString().trim('"'))
        assertFalse(peer.body!!.containsKey("proof"))
    }

    @Test
    fun `A2A rejects legacy proof outside the current contract`() = runTest {
        initializeSdk()
        sdk.registerGroupMessageListener(GroupMessageListener { _, _, _ -> })
        runtime.deliverGroupConfig(groupConfig())

        val error = runCatching {
            sdk.handleA2aMessage(buildJsonObject {
                put("message_id", "message-1")
                put("group_id", "g1")
                put("src_agent_id", PEER_ID)
                put("dst_agent_id", LOCAL_ID)
                put("type", "text")
                put("task_id", "task-patrol")
                put("timestamp", "2026-08-28T00:00:00Z")
                put("payload", buildJsonObject { put("hello", "world") })
                put("proof", buildJsonObject { put("jws", "legacy") })
            })
        }.exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.INVALID_ARGUMENT, error.code)
        assertEquals("proof", error.field)
    }

    @Test
    fun `listener reject does not roll back committed cache and routes`() = runTest {
        initializeSdk()
        sdk.registerNetworkMessageListener(NetworkMessageListener { _, _ ->
            NetworkMessageAction.REJECT
        })

        val action = runtime.deliverGroupConfig(groupConfig())

        assertEquals(NetworkMessageAction.ACK, action)
        assertNotNull(sdk.getGroupSnapshot("g1"))
        assertEquals(setOf("8.8.8.8"), tunnel.groupPeers["g1"])
    }

    @Test
    fun `port must be a decimal string in range`() = runTest {
        initializeSdk()
        sdk.registerNetworkMessageListener(NetworkMessageListener { _, _ ->
            NetworkMessageAction.ACK
        })

        val error = runCatching {
            runtime.deliverGroupConfig(groupConfig(peerPort = "65536"))
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

        val error = runCatching { runtime.deliverGroupConfig(invalid) }
            .exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.GROUP_CONFIG_INVALID, error.code)
    }

    @Test
    fun `send without committed config never falls back`() = runTest {
        initializeSdk()

        val error = runCatching {
            sdk.sendMessage(
                "g1",
                PEER_ID,
                buildJsonObject { put("hello", "world") },
                messageType = "text",
                taskId = "task-patrol",
            )
        }.exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.GROUP_NOT_ACTIVE, error.code)
        assertFalse(peer.called)
    }

    @Test
    fun `offloading video delegates to configured media adapter`() = runTest {
        initializeSdk()

        val session = sdk.createOffloadingSession(
            LOCAL_ID,
            workloadType = "video_rendering",
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
        assertEquals(
            setOf(
                "request_id", "agent_id", "workload_type", "preferred_sandbox_id",
                "timestamp", "proof",
            ),
            runtime.lastBody!!.keys,
        )
        assertEquals(
            "video_rendering",
            runtime.lastBody!!["workload_type"]!!.jsonPrimitive.content,
        )
        UUID.fromString(runtime.lastBody!!["request_id"]!!.jsonPrimitive.content)
        assertFalse(runtime.lastBody!!.containsKey("task_type"))
        assertEquals(
            "2026-08-21T09:00:00Z",
            runtime.lastBody!!["timestamp"]!!.jsonPrimitive.content,
        )
        assertEquals(
            "test-proof",
            runtime.lastBody!!["proof"]!!.jsonObject["jws"]!!
                .jsonPrimitive.content,
        )
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
        UUID.fromString(runtime.lastBody!!["request_id"].toString().trim('"'))
        assertFalse(runtime.lastBody!!.containsKey("request_type"))
    }

    @Test
    fun `identity application sends exact required HTTP fields`() = runTest {
        initializeSdk()

        val profile = sdk.applyIdentity(
            owner = "Alice",
            name = "AliceAgent",
            description = "AgentModel-X",
            metadata = buildJsonObject {
                put("region", "CN")
                put("os", "Android")
                put("version", "0.12.0")
            },
        )

        assertEquals(LOCAL_ID, profile.agentId)
        assertEquals("POST", runtime.lastMethod)
        assertEquals("/idm/v1/identity-applications", runtime.lastPath)
        UUID.fromString(runtime.lastBody!!.getValue("request_id").jsonPrimitive.content)
        assertEquals("Alice", runtime.lastBody!!.getValue("owner").jsonPrimitive.content)
        assertEquals(
            FakeDevicePublicKeyProvider.publicKeyBase64,
            runtime.lastBody!!.getValue("public_key").jsonPrimitive.content,
        )
        assertEquals(
            "test-signature",
            runtime.lastBody!!.getValue("signature").jsonPrimitive.content,
        )
        assertFalse(runtime.lastBody!!.containsKey("proof"))
    }

    @Test
    fun `identity application accepts string metadata and normalizes order`() = runTest {
        initializeSdk()

        sdk.applyIdentity(
            owner = "Alice",
            name = "AliceAgent",
            description = "AgentModel-X",
            metadata = buildJsonObject {
                put("zone", "north")
                put("version", "0.12.0")
                put("os", "Android")
                put("region", "CN")
                put("platform", "edge")
            },
        )

        assertEquals(
            "{\"region\":\"CN\",\"os\":\"Android\",\"version\":\"0.12.0\"," +
                "\"platform\":\"edge\",\"zone\":\"north\"}",
            runtime.lastBody!!.getValue("metadata").toString(),
        )
    }

    @Test
    fun `identity application rejects non-string metadata value`() = runTest {
        initializeSdk()

        val error = runCatching {
            sdk.applyIdentity(
                owner = "Alice",
                name = "AliceAgent",
                description = "AgentModel-X",
                metadata = buildJsonObject {
                    put("region", "CN")
                    put("os", "Android")
                    put("version", "0.12.0")
                    put("priority", 1)
                },
            )
        }.exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.INVALID_ARGUMENT, error.code)
        assertEquals("metadata", error.field)
    }

    @Test
    fun `AgentCard accepts existing VCs and raw capabilities`() = runTest {
        initializeSdk()
        val existing = buildJsonObject { put("id", "vc0") }

        sdk.registerCapabilities(
            agentId = LOCAL_ID,
            priority = 2,
            credentials = listOf(existing),
            capabilities = listOf("robot-control", "voice"),
        )

        assertEquals("POST", runtime.lastMethod)
        assertEquals("/arf/v1/agent-cards", runtime.lastPath)
        UUID.fromString(runtime.lastBody!!["request_id"].toString().trim('"'))
        assertEquals(
            "http://8.8.8.7:4001/A2A/message",
            runtime.lastBody!!.getValue("service_endpoints").jsonPrimitive.content,
        )
        val vcList = runtime.lastBody!!.getValue("vc_list").jsonArray
        assertEquals(3, vcList.size)
        assertEquals("vc0", vcList[0].jsonObject.getValue("id").jsonPrimitive.content)
        assertEquals(
            listOf("robot-control", "voice"),
            vcList.drop(1).map {
                it.jsonObject.getValue("claims").jsonObject
                    .getValue("skill_name").jsonPrimitive.content
            },
        )
        assertTrue(vcList.drop(1).all {
            it.jsonObject.getValue("proof").jsonObject
                .getValue("jws").jsonPrimitive.content.isNotBlank()
        })
    }

    @Test
    fun `discovery uses current request and AgentCard fields`() = runTest {
        initializeSdk()

        val agents = sdk.discoverAgents(
            agentId = LOCAL_ID,
            taskDescription = "Patrol Area A",
            requiredSkills = listOf("patrol"),
        )

        assertEquals("/arf/v1/agent-discoveries", runtime.lastPath)
        assertFalse(runtime.lastBody!!.containsKey("task_id"))
        assertEquals(1, agents.size)
        assertEquals(PEER_ID, agents.single().agentId)
        assertEquals(
            "http://agent-b:4001/A2A/message",
            agents.single().serviceEndpoints,
        )
        assertEquals(listOf("patrol"), agents.single().skills)
    }

    @Test
    fun `create group requires and sends dnn`() = runTest {
        initializeSdk()

        val group = sdk.createGroup(
            agentId = LOCAL_ID,
            targetAgentIds = listOf(PEER_ID),
            groupName = "patrol-group",
            dnn = "internet",
            maxMembers = 2,
        )

        assertEquals("g1", group.groupId)
        assertEquals("/acf/v1/agents-grouping", runtime.lastPath)
        assertEquals(
            "internet",
            runtime.lastBody!!.getValue("group_config").jsonObject
                .getValue("dnn").jsonPrimitive.content,
        )
    }

    @Test
    fun `create group rejects blank dnn`() = runTest {
        initializeSdk()

        val error = runCatching {
            sdk.createGroup(
                agentId = LOCAL_ID,
                targetAgentIds = listOf(PEER_ID),
                groupName = "patrol-group",
                dnn = "   ",
            )
        }.exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.INVALID_ARGUMENT, error.code)
        assertEquals("dnn", error.field)
    }

    private suspend fun initializeSdk() {
        val result = sdk.initialize(
            agentRuntimeIp = "192.168.3.10",
            agentRuntimePort = 8080,
            localVlanIp = "192.168.1.10",
            localTcpPort = 4001,
            localUdpPort = 28443,
            masqueServerUrl = "https://192.168.3.10:4433",
        )
        assertEquals("8.8.8.7:4001", result.agentTcpEndpoint)
        assertEquals("8.8.8.7/32", result.agentTunCidr)
        assertEquals("8.8.8.7/32", tunnel.establishedConfiguration?.agentTunCidr)
        assertTrue(tunnel.establishedConfiguration?.routes?.isEmpty() == true)
        assertEquals(tunnel.clientIdentityDirectory, masque.configuration?.identityDirectory)
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
            put("agent1", member(LOCAL_ID, "Agent A", "8.8.8.7", "4001"))
            put("not-an-id", member(PEER_ID, "Agent B", "8.8.8.8", peerPort))
        })
        put("proof", buildJsonObject { put("jws", "test") })
    }

    private fun member(
        id: String,
        name: String,
        ip: String,
        tcpPort: String,
    ): JsonObject = buildJsonObject {
        put("agent_id", id)
        put("agent_name", name)
        put("skills", buildJsonArray { add(JsonPrimitive("text")) })
        put("agent_ip", ip)
        put("service_endpoints", "http://agent.example:$tcpPort/A2A/message")
    }

    private fun testPrivateKeyPem(): ByteArray {
        val keyPair = KeyPairGenerator.getInstance("EC").run {
            initialize(ECGenParameterSpec("secp256r1"))
            generateKeyPair()
        }
        return buildString {
            appendLine("-----BEGIN PRIVATE KEY-----")
            appendLine(
                Base64.getMimeEncoder(64, byteArrayOf('\n'.code.toByte()))
                    .encodeToString(keyPair.private.encoded)
            )
            appendLine("-----END PRIVATE KEY-----")
        }.toByteArray(Charsets.US_ASCII)
    }

    private class FakeTunnel : TunnelController {
        override val tunFd: Int = 42
        override val clientIdentityDirectory: String = "/tmp/agent-sdk-test-identity"
        val groupPeers = mutableMapOf<String, Set<String>>()
        var establishedConfiguration: TunnelConfiguration? = null
        private var swapper: (suspend (Int) -> Unit)? = null

        override suspend fun establish(configuration: TunnelConfiguration) {
            establishedConfiguration = configuration
        }
        override suspend fun replaceGroupPeers(groupId: String, peerIps: Set<String>) {
            if (peerIps.isEmpty()) groupPeers.remove(groupId) else groupPeers[groupId] = peerIps
        }
        override fun currentAllowedPeerIps(): Set<String> = groupPeers.values.flatten().toSet()
        override fun setTunFdSwapper(swapper: suspend (Int) -> Unit) { this.swapper = swapper }
        override suspend fun close() = Unit
    }

    private class FakeMasque : MasqueTransport {
        override var connected: Boolean = false
        var configuration: MasqueConfiguration? = null
        override suspend fun start(tunFd: Int, configuration: MasqueConfiguration) {
            this.configuration = configuration
            connected = true
        }
        override suspend fun replaceTunFd(tunFd: Int) = Unit
        override suspend fun close() { connected = false }
    }

    private class FakeRuntime : RuntimeTransport {
        var lastMethod = ""
        var lastPath = ""
        var lastBody: JsonObject? = null
        var ueInfoRequests = 0
        var downlinkHandler: (suspend (String, Int, JsonObject) -> NetworkMessageAction)? = null

        override suspend fun getUeAgentIp(): String {
            ueInfoRequests += 1
            return "8.8.8.7"
        }

        override suspend fun startDownlink(
            handler: suspend (String, Int, JsonObject) -> NetworkMessageAction,
        ) { downlinkHandler = handler }
        suspend fun deliverDownlink(
            messageType: String,
            payload: JsonObject,
            transactionId: Int = 49,
        ): NetworkMessageAction = downlinkHandler!!(messageType, transactionId, payload)
        suspend fun deliverGroupConfig(payload: JsonObject): NetworkMessageAction =
            deliverDownlink("ACN_AGENT_GROUPING_NOTIFICATION", payload)
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
            } else if (path == "/idm/v1/identity-applications") {
                buildJsonObject {
                    put("result", "success")
                    put("agent_id", LOCAL_ID)
                    put("vc0", buildJsonObject {
                        put("claims", buildJsonObject { put("agent_name", "AliceAgent") })
                    })
                }
            } else if (path == "/arf/v1/agent-discoveries") {
                buildJsonObject {
                    put("task_description", "Patrol Area A")
                    put("result", buildJsonArray {
                        add(buildJsonObject {
                            put("agent_card", buildJsonObject {
                                put("agent_id", PEER_ID)
                                put("service_endpoints", "http://agent-b:4001/A2A/message")
                                put("skills", buildJsonArray { add(JsonPrimitive("patrol")) })
                            })
                            put("priority", 1)
                        })
                    })
                    put("timestamp", "2026-08-21T09:00:01.000Z")
                }
            } else if (path == "/acf/v1/agents-grouping") {
                buildJsonObject {
                    put("status", "grouped")
                    put("group_id", "g1")
                }
            } else {
                buildJsonObject { }
            }
        }
        override suspend fun close() = Unit
    }

    private class FakeServer : LocalServer {
        override suspend fun start(
            physicalIp: String,
            agentIp: String,
            tcpPort: Int,
            udpPort: Int,
            onA2aMessage: suspend (JsonObject) -> Unit,
        ) = Unit
        override suspend fun close() = Unit
    }

    private class FakePeer : PeerMessenger {
        var called = false
        var ip = ""
        var port = 0
        var body: JsonObject? = null
        override suspend fun send(
            endpoint: String,
            body: JsonObject,
            timeoutMillis: Long,
        ): JsonObject {
            called = true
            val url = java.net.URI(endpoint)
            this.ip = url.host
            this.port = url.port
            this.body = body
            return buildJsonObject { put("status", "OK") }
        }
    }

    private object FakeMessageSigner : MessageSigner {
        override suspend fun signA2a(payload: JsonObject): JsonObject = buildJsonObject {
            put("jws", "test-message-signature")
        }
    }

    private object FakeControlAuthenticator : ControlRequestAuthenticator {
        override suspend fun authenticate(path: String, payload: JsonObject): JsonObject =
            if (path == "/idm/v1/identity-applications") {
                buildJsonObject {
                    put("timestamp", "2026-08-21T09:00:00Z")
                    put("signature", "test-signature")
                    put("signature_encoding", "base64")
                }
            } else {
                buildJsonObject {
                    put("timestamp", "2026-08-21T09:00:00Z")
                    put("proof", buildJsonObject { put("jws", "test-proof") })
                }
            }
    }

    private object FakeDevicePublicKeyProvider : DevicePublicKeyProvider {
        override fun ensure() = Unit
        override val publicKeyBase64 =
            "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEaxfR8uEsQkf4vOblY6RA8ncD" +
                "fYEt6zOg9KE5RdiYwpZP40Li/hp/m47n60p8D54WK84zV2sxXs7LtkBoN79R9Q=="
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
