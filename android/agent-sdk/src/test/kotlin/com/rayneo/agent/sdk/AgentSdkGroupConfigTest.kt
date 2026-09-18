package com.rayneo.agent.sdk

import com.rayneo.agent.sdk.model.AgentProfile
import com.rayneo.agent.sdk.model.AgentLifecycleState
import com.rayneo.agent.sdk.model.AcnContext
import com.rayneo.agent.sdk.model.AudioControlActionRequest
import com.rayneo.agent.sdk.model.AudioTranscriptionRequest
import com.rayneo.agent.sdk.model.ComputeConstraints
import com.rayneo.agent.sdk.model.ComputeInputFormat
import com.rayneo.agent.sdk.model.ComputeRequestType
import com.rayneo.agent.sdk.model.ComputeResources
import com.rayneo.agent.sdk.model.ComputeSessionRequest
import com.rayneo.agent.sdk.model.ComputingSession
import com.rayneo.agent.sdk.model.ControlAction
import com.rayneo.agent.sdk.model.ControlActionRequest
import com.rayneo.agent.sdk.model.ControlInputType
import com.rayneo.agent.sdk.model.NetworkMessageAction
import com.rayneo.agent.sdk.model.NetworkMessageType
import com.rayneo.agent.sdk.transport.LocalServer
import com.rayneo.agent.sdk.transport.LocalAddressResolver
import com.rayneo.agent.sdk.transport.ControlRequestAuthenticator
import com.rayneo.agent.sdk.transport.DevicePublicKeyProvider
import com.rayneo.agent.sdk.transport.GroupMessageListener
import com.rayneo.agent.sdk.transport.MediaOffloadAdapter
import com.rayneo.agent.sdk.transport.LocalProcessedVideo
import com.rayneo.agent.sdk.transport.PreparedMediaConnection
import com.rayneo.agent.sdk.transport.SandboxTransport
import com.rayneo.agent.sdk.transport.MessageSigner
import com.rayneo.agent.sdk.transport.MasqueConfiguration
import com.rayneo.agent.sdk.transport.MasqueTransport
import com.rayneo.agent.sdk.transport.NetworkMessageListener
import com.rayneo.agent.sdk.transport.PeerMessenger
import com.rayneo.agent.sdk.transport.ProofVerifier
import com.rayneo.agent.sdk.transport.RuntimeTransport
import com.rayneo.agent.sdk.transport.RuntimeHttpResponse
import com.rayneo.agent.sdk.transport.TunnelConfiguration
import com.rayneo.agent.sdk.transport.TunnelController
import com.rayneo.agent.sdk.transport.VideoTrack
import com.rayneo.agent.sdk.transport.VideoUploadHandle
import com.rayneo.agent.sdk.security.TestCapabilityVcIssuer
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.add
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
    private lateinit var sandbox: FakeSandbox
    private lateinit var testCapabilityIssuer: TestCapabilityVcIssuer
    private lateinit var addressResolver: FakeLocalAddressResolver
    private lateinit var sdk: AgentSdk
    private val runtimeTargets = mutableListOf<Pair<String, Int>>()

    @Test
    fun `computing API replaces legacy offloading API`() {
        val publicMethods = AgentSdk::class.java.methods
        assertFalse(publicMethods.any { it.name == "createOffloadingSession" })
        val upload = publicMethods.single { it.name == "startVideoUpload" }
        assertEquals(8, upload.parameterCount)
        assertEquals(String::class.java, upload.parameterTypes.first())
        val processed = publicMethods.single { it.name == "getProcessedVideoStream" }
        assertEquals(String::class.java, processed.parameterTypes.first())
        val create = publicMethods.single { it.name == "createComputingSession" }
        assertTrue(publicMethods.any { it.name == "updateRecognitionTarget" })
        assertTrue(publicMethods.any { it.name == "getRecognitionTarget" })
        assertTrue(publicMethods.any { it.name == "createControlAction" })
        assertTrue(publicMethods.any { it.name == "createAudioControlAction" })
        assertTrue(publicMethods.any { it.name == "transcribeAudio" })
        assertTrue(publicMethods.any { it.name == "recognizeIntent" })
        assertTrue(publicMethods.any { it.name == "getControlAction" })
        assertEquals(ComputeSessionRequest::class.java, create.parameterTypes[0])
    }

    @Test
    fun `computing creation rejects invalid resource conditions`() = runTest {
        initializeSdk()

        val cpuError = runCatching {
            sdk.createComputingSession(createComputeRequest(cpuMillicores = -1))
        }.exceptionOrNull() as AgentSdkException
        assertEquals(ErrorCode.INVALID_ARGUMENT, cpuError.code)
        assertEquals("constraints.resources.cpu_millicores", cpuError.field)
    }

    @Before
    fun setUp() {
        tunnel = FakeTunnel()
        masque = FakeMasque()
        runtime = FakeRuntime()
        server = FakeServer()
        peer = FakePeer()
        media = FakeMedia()
        sandbox = FakeSandbox()
        addressResolver = FakeLocalAddressResolver()
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
            runtimeFactory = { host, port ->
                runtimeTargets += host to port
                runtime
            },
            localServerFactory = { server },
            localAddressResolver = addressResolver,
            mediaOffloadAdapter = media,
            sandboxTransport = sandbox,
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
    fun `identical group config replay ACKs without route or listener side effects`() = runTest {
        initializeSdk()
        var listenerCalls = 0
        sdk.registerNetworkMessageListener(NetworkMessageListener { _, _ ->
            listenerCalls += 1
            NetworkMessageAction.ACK
        })
        val timestamp = Instant.parse("2026-09-03T07:00:00Z")
        val payload = groupConfig(timestamp = timestamp)

        assertEquals(NetworkMessageAction.ACK, runtime.deliverGroupConfig(payload))
        assertEquals(NetworkMessageAction.ACK, runtime.deliverGroupConfig(payload))

        assertEquals(1, listenerCalls)
        assertEquals(1, tunnel.replacements.size)
        assertEquals(1L, sdk.getGroupSnapshot("g1")!!.generation)
    }

    @Test
    fun `newer identical group config commits and notifies listener`() = runTest {
        initializeSdk()
        var listenerCalls = 0
        sdk.registerNetworkMessageListener(NetworkMessageListener { _, _ ->
            listenerCalls += 1
            NetworkMessageAction.ACK
        })
        val timestamp = Instant.parse("2026-09-03T07:00:00Z")

        runtime.deliverGroupConfig(groupConfig(timestamp = timestamp))
        val action = runtime.deliverGroupConfig(
            groupConfig(timestamp = timestamp.plusSeconds(1)),
        )

        assertEquals(NetworkMessageAction.ACK, action)
        assertEquals(2, listenerCalls)
        assertEquals(2, tunnel.replacements.size)
        assertEquals(2L, sdk.getGroupSnapshot("g1")!!.generation)
    }

    @Test
    fun `same timestamp with different group content is rejected`() = runTest {
        initializeSdk()
        var listenerCalls = 0
        sdk.registerNetworkMessageListener(NetworkMessageListener { _, _ ->
            listenerCalls += 1
            NetworkMessageAction.ACK
        })
        val timestamp = Instant.parse("2026-09-03T07:00:00Z")
        runtime.deliverGroupConfig(groupConfig(timestamp = timestamp))

        val error = runCatching {
            runtime.deliverGroupConfig(
                groupConfig(timestamp = timestamp, peerIp = "8.8.8.9"),
            )
        }.exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.GROUP_CONFIG_STALE, error.code)
        assertEquals(1, listenerCalls)
        assertEquals(1, tunnel.replacements.size)
        assertEquals("8.8.8.8", sdk.getGroupSnapshot("g1")!!.membersByAgentId[PEER_ID]!!.agentIp)
    }

    @Test
    fun `initialize queries UE info and registers runtime downlink websocket`() = runTest {
        initializeSdk()

        assertEquals(1, runtime.ueInfoRequests)
        assertEquals("", runtime.lastPath)
        assertNotNull(runtime.downlinkHandler)
        assertEquals("8.8.8.7/32", tunnel.establishedConfiguration?.agentTunCidr)
        assertEquals(1, addressResolver.calls)
        assertEquals("192.168.1.10", masque.configuration?.localVlanIp)
        assertEquals("8.8.8.7", server.agentIp)
    }

    @Test
    fun `TUN fd replacement recreates local ingress after MASQUE packet swap`() = runTest {
        val events = mutableListOf<String>()
        val localServers = mutableListOf<FakeServer>()
        masque = FakeMasque(events)
        sdk = AgentSdk(
            tunnelController = tunnel,
            masqueTransport = masque,
            proofVerifier = ProofVerifier { },
            controlRequestAuthenticator = FakeControlAuthenticator,
            devicePublicKeyProvider = FakeDevicePublicKeyProvider,
            messageSigner = FakeMessageSigner,
            peerMessenger = peer,
            runtimeFactory = { _, _ -> runtime },
            localServerFactory = {
                val number = localServers.size + 1
                FakeServer(
                    onStart = { events += "server-start-$number" },
                    onClose = { events += "server-close-$number" },
                ).also(localServers::add)
            },
            localAddressResolver = addressResolver,
            mediaOffloadAdapter = media,
            testCapabilityVcIssuer = testCapabilityIssuer,
        )
        sdk.importTestCapabilityIssuerPrivateKey(testPrivateKeyPem())
        initializeSdk()
        events.clear()

        tunnel.simulateTunReplacement(43)

        assertEquals(2, localServers.size)
        assertEquals(1, localServers.first().closeCount)
        assertEquals(1, localServers.last().startCount)
        assertEquals(
            listOf("masque-replace-43", "server-close-1", "server-start-2"),
            events,
        )
    }

    @Test
    fun `explicit outer source address bypasses automatic route selection`() = runTest {
        val result = sdk.initialize(
            agentRuntimeIp = "192.168.3.10",
            agentRuntimePort = 8080,
            localVlanIp = "192.168.9.10",
            localTcpPort = 4001,
            localUdpPort = 28443,
            masqueServerUrl = "https://192.168.3.10:4433",
        )

        assertEquals(0, addressResolver.calls)
        assertEquals("192.168.9.10", masque.configuration?.localVlanIp)
        assertEquals("192.168.9.10", result.masqueOuterSourceIp)
        assertEquals("8.8.8.7:4001", result.localTcpEndpoint)
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
                put("group_info", buildJsonObject {
                    put("target_agent_id", "agent-b")
                    put("group_id", "group-a-b")
                    put("group_name", "task-patrol")
                })
                put("group_administrator", buildJsonObject { put("agent_id", "a1") })
            },
        )

        assertEquals("ACCEPT", action!!["result"]!!.jsonPrimitive.content)
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
    fun `group config ignores unknown supi and does not require it`() = runTest {
        initializeSdk()

        val action = runtime.deliverGroupConfig(groupConfig(peerSupi = null))

        assertEquals(NetworkMessageAction.ACK, action)
        assertNotNull(sdk.getGroupSnapshot("g1"))
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
    fun `duplicate A2A message id is acknowledged without redispatch`() = runTest {
        initializeSdk()
        runtime.deliverGroupConfig(groupConfig())
        var deliveries = 0
        sdk.registerGroupMessageListener(GroupMessageListener { _, _, _ -> deliveries += 1 })
        val message = buildJsonObject {
            put("message_id", "message-retried-after-tun-replacement")
            put("group_id", "g1")
            put("src_agent_id", PEER_ID)
            put("dst_agent_id", LOCAL_ID)
            put("type", "computing_video_session")
            put("task_id", "computing:css-001")
            put("timestamp", "2026-09-14T13:37:05Z")
            put("payload", buildJsonObject {
                put("compute_service_session_id", "css-001")
            })
        }

        sdk.handleA2aMessage(message)
        sdk.handleA2aMessage(message)

        assertEquals(1, deliveries)
    }

    @Test
    fun `computing create uses formal path and exact body`() = runTest {
        initializeSdk()
        runtime.deliverGroupConfig(groupConfig(includeSecondPeer = true))

        val status = sdk.createComputingSession(createComputeRequest())

        assertEquals("css-001", status.computeServiceSessionId)
        val body = runtime.bodies.getValue("/v1/computing/session-requests")
        assertEquals("COMPUTE_SESSION_REQUEST", body["message_type"]!!.jsonPrimitive.content)
        assertEquals("CREATE", body["request_type"]!!.jsonPrimitive.content)
        assertEquals("STRUCTURED", body["input_format"]!!.jsonPrimitive.content)
        assertEquals("create-001", body["request_id"]!!.jsonPrimitive.content)
        assertEquals("g1", body["acn_context"]!!.jsonObject["group_id"]!!.jsonPrimitive.content)
        assertEquals("dog-vision", body["constraints"]!!.jsonObject["capability_id"]!!.jsonPrimitive.content)
        assertFalse(body.containsKey("proof"))
        assertFalse(body.containsKey("timestamp"))
    }

    @Test
    fun `computing create accepts correlated C04 while HTTP response is pending`() = runTest {
        initializeSdk()
        runtime.deliverGroupConfig(groupConfig(includeSecondPeer = true))
        runtime.computeRequestHandler = { body ->
            runtime.deliverDownlink(
                "COMPUTE_SESSION_STATUS",
                buildJsonObject {
                    put("request_id", body["request_id"]!!)
                    put("compute_service_session_id", "css-async-001")
                    put("status_revision", "7")
                    put("status", "ACTIVE")
                    put("cause", "")
                },
            )
            CompletableDeferred<RuntimeHttpResponse>().await()
        }

        val status = sdk.createComputingSession(createComputeRequest(), timeoutSeconds = 20.0)

        assertEquals("css-async-001", status.computeServiceSessionId)
        assertEquals("ACTIVE", status.status)
        assertEquals("7", status.statusRevision)
    }

    @Test
    fun `older HTTP C04 cannot regress a newer WebSocket C04`() = runTest {
        initializeSdk()
        runtime.deliverGroupConfig(groupConfig(includeSecondPeer = true))
        runtime.computeRequestHandler = { body ->
            runtime.deliverDownlink(
                "COMPUTE_SESSION_STATUS",
                buildJsonObject {
                    put("request_id", body["request_id"]!!)
                    put("compute_service_session_id", "css-001")
                    put("status_revision", "7")
                    put("status", "ACTIVE")
                    put("cause", "")
                },
            )
            RuntimeHttpResponse(
                202,
                buildJsonObject {
                    put("message_type", "COMPUTE_SESSION_STATUS")
                    put("request_id", body["request_id"]!!)
                    put("compute_service_session_id", "css-001")
                    put("status_revision", "2")
                    put("status", "ACCEPTED")
                    put("cause", "")
                },
            )
        }

        val first = sdk.createComputingSession(createComputeRequest())
        val replay = sdk.createComputingSession(createComputeRequest())

        assertEquals("ACTIVE", first.status)
        assertEquals("7", first.statusRevision)
        assertEquals(first, replay)
    }

    @Test
    fun `computing lifecycle operations use the formal endpoint and target fields`() = runTest {
        initializeSdk()

        sdk.queryComputingSession(
            ComputeSessionRequest(
                messageType = "COMPUTE_SESSION_REQUEST",
                requestType = ComputeRequestType.QUERY,
                inputFormat = ComputeInputFormat.STRUCTURED,
                requestId = "query-001",
                targetRequestId = "create-001",
            ),
        )
        var body = runtime.bodies.getValue("/v1/computing/session-requests")
        assertEquals("QUERY", body["request_type"]!!.jsonPrimitive.content)
        assertEquals("create-001", body["target_request_id"]!!.jsonPrimitive.content)
        assertFalse(body.containsKey("acn_context"))

        sdk.cancelComputingSession(
            ComputeSessionRequest(
                messageType = "COMPUTE_SESSION_REQUEST",
                requestType = ComputeRequestType.CANCEL,
                inputFormat = ComputeInputFormat.STRUCTURED,
                requestId = "cancel-001",
                computeServiceSessionId = "css-001",
            ),
        )
        body = runtime.bodies.getValue("/v1/computing/session-requests")
        assertEquals("CANCEL", body["request_type"]!!.jsonPrimitive.content)
        assertEquals("css-001", body["compute_service_session_id"]!!.jsonPrimitive.content)

        sdk.releaseComputingSession(
            ComputeSessionRequest(
                messageType = "COMPUTE_SESSION_REQUEST",
                requestType = ComputeRequestType.RELEASE,
                inputFormat = ComputeInputFormat.STRUCTURED,
                requestId = "release-001",
                computeServiceSessionId = "css-001",
            ),
        )
        body = runtime.bodies.getValue("/v1/computing/session-requests")
        assertEquals("RELEASE", body["request_type"]!!.jsonPrimitive.content)
        assertEquals("css-001", body["compute_service_session_id"]!!.jsonPrimitive.content)
        assertFalse(body.containsKey("target_request_id"))
    }

    @Test
    fun `C02 is accepted internally and media uses cached sandbox endpoint`() = runTest {
        initializeSdk()
        val ack = runtime.deliverDownlink(
            "COMPUTE_CONNECT_CONFIG",
            computeConnectConfig("producer"),
        )!!

        assertTrue(ack["accepted"]!!.jsonPrimitive.content.toBoolean())
        assertEquals("", ack["cause"]!!.jsonPrimitive.content)
        val upload = sdk.startVideoUpload("css-001", cameraId = "2")
        assertEquals("camera-track-1", upload.trackId)
        assertEquals("2", media.cameraId)
        assertEquals("POST", sandbox.requests.last().first)
        assertEquals(
            "http://8.8.8.9:8788/v1/media-connections",
            sandbox.requests.last().second,
        )
        val body = checkNotNull(sandbox.requests.last().third)
        assertEquals(
            "producer",
            body["computing_context"]!!.jsonObject["role"]!!.jsonPrimitive.content,
        )
        assertTrue(body["offer"]!!.jsonObject["sdp"]!!.jsonPrimitive.content.contains("a=sendonly"))
        assertEquals(
            setOf("8.8.8.9", "8.8.8.10"),
            tunnel.groupPeers["computing:binding-css-001"],
        )
    }

    @Test
    fun `consumer uses the same formal media resource and closes it with DELETE`() = runTest {
        initializeSdk()
        runtime.deliverDownlink("COMPUTE_CONNECT_CONFIG", computeConnectConfig("consumer"))

        val stream = sdk.getProcessedVideoStream("css-001")

        assertEquals("processed-track-1", stream.track.trackId)
        val body = checkNotNull(sandbox.requests.last().third)
        assertEquals(
            "consumer",
            body["computing_context"]!!.jsonObject["role"]!!.jsonPrimitive.content,
        )
        assertTrue(body["offer"]!!.jsonObject["sdp"]!!.jsonPrimitive.content.contains("a=recvonly"))
        stream.close()
        assertEquals("CLOSED", stream.state)
        assertEquals("DELETE", sandbox.requests.last().first)
        assertEquals(
            "http://8.8.8.9:8788/v1/media-connections/media-consumer-001",
            sandbox.requests.last().second,
        )
        assertEquals(setOf("8.8.8.9"), tunnel.groupPeers["computing:binding-css-001"])
    }

    @Test
    fun `terminal C04 preserves cause revision and result in media failure`() = runTest {
        initializeSdk()
        runtime.deliverDownlink(
            "COMPUTE_SESSION_STATUS",
            buildJsonObject {
                put("request_id", "create-001")
                put("compute_service_session_id", "css-001")
                put("status_revision", "7")
                put("status", "FAILED")
                put("cause", "resource-activation-failed")
                put("result", buildJsonObject {
                    put("failed_component", "sandbox")
                })
            },
        )

        val error = runCatching {
            sdk.getProcessedVideoStream("css-001")
        }.exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.COMPUTING_SESSION_INVALID, error.code)
        assertTrue(error.message.orEmpty().contains("state FAILED"))
        assertTrue(error.message.orEmpty().contains("cause=resource-activation-failed"))
        assertTrue(error.message.orEmpty().contains("status_revision=7"))
        assertTrue(error.message.orEmpty().contains("failed_component"))
        assertTrue(error.message.orEmpty().contains("sandbox"))
    }

    @Test
    fun `terminal C04 takes precedence over an already cached C02`() = runTest {
        initializeSdk()
        runtime.deliverDownlink("COMPUTE_CONNECT_CONFIG", computeConnectConfig("consumer"))
        runtime.deliverDownlink(
            "COMPUTE_SESSION_STATUS",
            computeStatus(status = "FAILED", revision = "7"),
        )

        val error = runCatching {
            sdk.getProcessedVideoStream("css-001")
        }.exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.COMPUTING_SESSION_INVALID, error.code)
        assertEquals(0, media.prepareCount)
    }

    @Test
    fun `C02 arriving after terminal C04 is rejected without installing route`() = runTest {
        initializeSdk()
        runtime.deliverDownlink(
            "COMPUTE_SESSION_STATUS",
            computeStatus(status = "FAILED", revision = "7"),
        )

        val ack = runtime.deliverDownlink(
            "COMPUTE_CONNECT_CONFIG",
            computeConnectConfig("consumer"),
        )!!

        assertFalse(ack["accepted"]!!.jsonPrimitive.content.toBoolean())
        assertEquals("session-closed", ack["cause"]!!.jsonPrimitive.content)
        assertFalse(tunnel.groupPeers.containsKey("computing:binding-css-001"))
    }

    @Test
    fun `C05 waits for an earlier C02 installation and then closes it`() = runTest {
        initializeSdk()
        val releaseRouteInstall = CompletableDeferred<Unit>()
        tunnel.computeReplacementGate = releaseRouteInstall
        val c02 = async {
            runtime.deliverDownlink("COMPUTE_CONNECT_CONFIG", computeConnectConfig("consumer"))!!
        }
        tunnel.computeReplacementStarted.await()
        val c05 = async {
            runtime.deliverDownlink("COMPUTE_SESSION_CLOSE", computeSessionClose("consumer"))!!
        }

        assertFalse(c05.isCompleted)
        releaseRouteInstall.complete(Unit)

        assertTrue(c02.await()["accepted"]!!.jsonPrimitive.content.toBoolean())
        assertTrue(c05.await()["closed"]!!.jsonPrimitive.content.toBoolean())
        assertFalse(tunnel.groupPeers.containsKey("computing:binding-css-001"))
    }

    @Test
    fun `media retry after public timeout reuses request id and offer`() = runTest {
        initializeSdk()
        runtime.deliverDownlink("COMPUTE_CONNECT_CONFIG", computeConnectConfig("producer"))
        sandbox.failuresRemaining = 2

        val firstError = runCatching { sdk.startVideoUpload("css-001") }.exceptionOrNull()
            as AgentSdkException
        assertTrue(firstError.retryable)
        val upload = sdk.startVideoUpload("css-001")

        assertEquals("camera-track-1", upload.trackId)
        assertEquals(1, media.prepareCount)
        val posts = sandbox.requests.filter { it.first == "POST" }
        assertEquals(3, posts.size)
        assertEquals(posts[0].third, posts[1].third)
        assertEquals(posts[1].third, posts[2].third)
    }

    @Test
    fun `C05 closes media and returns replayable C06`() = runTest {
        initializeSdk()
        runtime.deliverDownlink("COMPUTE_CONNECT_CONFIG", computeConnectConfig("producer"))
        val upload = sdk.startVideoUpload("css-001")
        val close = buildJsonObject {
            put("compute_service_session_id", "css-001")
            put("compute_instance_id", "ci-001")
            put("binding_ref", "binding-css-001")
            put("role", "producer")
            put("receiver_agent_id", LOCAL_ID)
            put("cause", "released")
        }

        val first = runtime.deliverDownlink("COMPUTE_SESSION_CLOSE", close)!!
        val second = runtime.deliverDownlink("COMPUTE_SESSION_CLOSE", close)!!

        assertEquals(first, second)
        assertTrue(first["closed"]!!.jsonPrimitive.content.toBoolean())
        assertEquals("STOPPED", upload.state)
        assertEquals("DELETE", sandbox.requests.last().first)
        assertEquals(
            "http://8.8.8.9:8788/v1/media-connections/media-producer-001",
            sandbox.requests.last().second,
        )
        assertFalse(tunnel.groupPeers.containsKey("computing:binding-css-001"))
    }

    @Test
    fun `identity removal preserves profile until C05 closes computing configuration`() = runTest {
        initializeSdk()
        runtime.deliverDownlink("COMPUTE_CONNECT_CONFIG", computeConnectConfig("consumer"))
        val profile = checkNotNull(sdk.localProfile)
        runtime.paths.clear()

        val deregisterFailure = runCatching {
            sdk.deregisterIdentity(profile.agentId, "normal")
        }.exceptionOrNull() as AgentSdkException
        val resetFailure = runCatching { sdk.resetAgent() }
            .exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.AGENT_STATE_INVALID, deregisterFailure.code)
        assertEquals(ErrorCode.AGENT_STATE_INVALID, resetFailure.code)
        assertEquals(profile, sdk.localProfile)
        assertTrue(runtime.paths.isEmpty())

        val closeWaiter = async { sdk.awaitComputingSessionClosed("css-001") }
        val close = runtime.deliverDownlink(
            "COMPUTE_SESSION_CLOSE",
            computeSessionClose("consumer"),
        )!!
        assertTrue(close["closed"]!!.jsonPrimitive.content.toBoolean())
        closeWaiter.await()

        val result = sdk.deregisterIdentity(profile.agentId, "normal")

        assertTrue(result.success)
        assertEquals(AgentLifecycleState.NO_IDENTITY, sdk.agentLifecycleState)
        assertEquals(null, sdk.localProfile)
        assertEquals(listOf("/acn-agent/v1/agent-deletions"), runtime.paths)
    }

    @Test
    fun `non terminal C04 blocks reset before C02 arrives`() = runTest {
        initializeSdk()
        val profile = checkNotNull(sdk.localProfile)
        runtime.deliverGroupConfig(groupConfig(includeSecondPeer = true))
        sdk.createComputingSession(createComputeRequest())

        val failure = runCatching { sdk.resetAgent() }
            .exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.AGENT_STATE_INVALID, failure.code)
        assertEquals(profile, sdk.localProfile)

        runtime.deliverDownlink(
            "COMPUTE_SESSION_STATUS",
            buildJsonObject {
                put("request_id", "cancel-001")
                put("compute_service_session_id", "css-001")
                put("status_revision", "3")
                put("status", "REQUEST_CANCELLED")
                put("cause", "cancelled")
            },
        )
        val result = sdk.resetAgent()

        assertTrue(result.success)
        assertEquals(AgentLifecycleState.NO_IDENTITY, sdk.agentLifecycleState)
    }

    @Test
    fun `in flight computing request blocks reset atomically`() = runTest {
        initializeSdk()
        runtime.deliverGroupConfig(groupConfig(includeSecondPeer = true))
        val profile = checkNotNull(sdk.localProfile)
        val requestStarted = CompletableDeferred<Unit>()
        val releaseResponse = CompletableDeferred<Unit>()
        runtime.computeRequestHandler = { body ->
            requestStarted.complete(Unit)
            releaseResponse.await()
            RuntimeHttpResponse(
                202,
                buildJsonObject {
                    put("message_type", "COMPUTE_SESSION_STATUS")
                    put("request_id", body["request_id"]!!)
                    put("compute_service_session_id", "css-001")
                    put("status_revision", "3")
                    put("status", "REQUEST_CANCELLED")
                    put("cause", "cancelled")
                },
            )
        }
        val create = async { sdk.createComputingSession(createComputeRequest()) }
        requestStarted.await()

        val failure = runCatching { sdk.resetAgent() }
            .exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.AGENT_STATE_INVALID, failure.code)
        assertEquals(profile, sdk.localProfile)

        releaseResponse.complete(Unit)
        assertEquals("REQUEST_CANCELLED", create.await().status)
        assertTrue(sdk.resetAgent().success)
    }

    @Test
    fun `recognition target uses C02 path and exact consumer context`() = runTest {
        initializeSdk()
        runtime.deliverDownlink("COMPUTE_CONNECT_CONFIG", computeConnectConfig("consumer"))

        val updated = sdk.updateRecognitionTarget(
            computeServiceSessionId = "css-001",
            requestId = "recognition-001",
            text = "寻找红色玩偶",
            language = "zh",
        )
        val fetched = sdk.getRecognitionTarget("css-001")

        assertEquals(updated, fetched)
        assertEquals("APPLIED", updated.status)
        assertEquals("1", updated.targetRevision)
        assertEquals("红色玩偶", updated.target.label)
        val update = sandbox.requests[sandbox.requests.lastIndex - 1]
        assertEquals("PUT", update.first)
        assertEquals(
            "http://8.8.8.9:8788/v1/recognition-targets/css-001",
            update.second,
        )
        val body = checkNotNull(update.third)
        assertEquals("recognition-001", body["request_id"]!!.jsonPrimitive.content)
        assertEquals(
            "consumer",
            body["computing_context"]!!.jsonObject["role"]!!.jsonPrimitive.content,
        )
        assertEquals(
            "寻找红色玩偶",
            body["input"]!!.jsonObject["text"]!!.jsonPrimitive.content,
        )
        assertEquals("GET", sandbox.requests.last().first)
        assertEquals(null, sandbox.requests.last().third)
    }

    @Test
    fun `C02 rejects recognition path without session placeholder`() = runTest {
        initializeSdk()
        val original = computeConnectConfig("consumer")
        val parameters = original["connection_parameters"]!!.jsonObject.toMutableMap()
        parameters["recognition_target_path_template"] =
            JsonPrimitive("/v1/recognition-targets/current")
        val payload = JsonObject(
            original.toMutableMap().apply {
                this["connection_parameters"] = JsonObject(parameters)
            },
        )

        val response = runtime.deliverDownlink("COMPUTE_CONNECT_CONFIG", payload)!!

        assertFalse(response["accepted"]!!.jsonPrimitive.content.toBoolean())
        assertEquals("invalid-request", response["cause"]!!.jsonPrimitive.content)
    }

    @Test
    fun `text control action uses formal Sandbox resource`() = runTest {
        initializeSdk()
        runtime.deliverDownlink("COMPUTE_CONNECT_CONFIG", computeConnectConfig("consumer"))

        val created = sdk.createControlAction(
            "css-001",
            ControlActionRequest(
                requestId = "control-search-001",
                inputType = ControlInputType.TEXT,
                text = "寻找杯子",
                language = "zh",
            ),
        )
        val fetched = sdk.getControlAction("css-001", created.actionId)

        assertEquals("action-001", created.actionId)
        assertEquals("RUNNING", created.status)
        assertEquals(ControlAction.search_object, created.normalizedAction)
        assertEquals("COMPLETED", fetched.status)
        assertEquals(null, fetched.computingContext)
        val create = sandbox.requests[sandbox.requests.lastIndex - 1]
        assertEquals("POST", create.first)
        assertEquals("http://8.8.8.9:8788/v1/control-actions", create.second)
        val body = checkNotNull(create.third)
        assertEquals("control-search-001", body["request_id"]!!.jsonPrimitive.content)
        assertEquals(
            "consumer",
            body["computing_context"]!!.jsonObject["role"]!!.jsonPrimitive.content,
        )
        assertEquals("TEXT", body["input"]!!.jsonObject["type"]!!.jsonPrimitive.content)
        assertEquals("寻找杯子", body["input"]!!.jsonObject["text"]!!.jsonPrimitive.content)
        assertEquals("GET", sandbox.requests.last().first)
        assertEquals(
            "http://8.8.8.9:8788/v1/control-actions/action-001",
            sandbox.requests.last().second,
        )
    }

    @Test
    fun `structured control action requires parameters`() = runTest {
        initializeSdk()

        val error = runCatching {
            sdk.createControlAction(
                "css-001",
                ControlActionRequest(
                    requestId = "control-movement-001",
                    inputType = ControlInputType.STRUCTURED,
                    action = ControlAction.movement,
                ),
            )
        }.exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.INVALID_ARGUMENT, error.code)
        assertEquals("parameters", error.field)
    }

    @Test
    fun `standalone ASR upload works before SDK initialization`() = runTest {
        val result = sdk.transcribeAudio(
            "http://sandbox.example:9004/api/v1/transcribe",
            AudioTranscriptionRequest(
                audio = byteArrayOf(1, 2, 3),
                fileName = "speech.m4a",
                contentType = "audio/mp4",
                requestId = "voice-task-001",
                language = "zh",
            ),
        )

        assertEquals("voice-task-001", result.requestId)
        assertEquals("派机器狗巡逻A区域", result.text)
        assertEquals("TASK", result.intent.type)
        assertEquals("A", result.intent.area)
        assertEquals(listOf("patrol", "camera"), result.requiredSkills)
        val upload = sandbox.uploads.single()
        assertEquals("http://sandbox.example:9004/api/v1/transcribe", upload.url)
        assertEquals("voice-task-001", upload.fields["request_id"])
        assertEquals(null, upload.fields["session_id"])
        assertEquals(null, upload.sourceIpv4)
    }

    @Test
    fun `standalone intent recognition normalizes patrol and extracts area before initialization`() =
        runTest {
            val result = sdk.recognizeIntent(
                "http://sandbox.example:8011/api/v1/intent",
                "派机器狗巡逻A区域",
            )

            assertEquals("security patrol", result.intent)
            assertEquals("patrol", result.scene)
            assertEquals("robot dog", result.executor)
            assertEquals("A", result.area)
            assertTrue(result.matched)
            assertEquals("rules", result.backend)
            val request = sandbox.requests.single()
            assertEquals("POST", request.first)
            assertEquals("http://sandbox.example:8011/api/v1/intent", request.second)
            assertEquals("派机器狗巡逻A区域", request.third!!["text"]!!.jsonPrimitive.content)
            assertEquals(null, sandbox.requestSourceIpv4.single())
        }

    @Test
    fun `standalone intent recognition accepts public nested intent contract`() = runTest {
        sandbox.intentResponse = buildJsonObject {
            put("status", "success")
            put("intent", buildJsonObject {
                put("executor", "robot dog")
                put("intent", "security patrol")
                put("area", "A")
                put("matched", true)
                put("backend", "qwen")
            })
        }

        val result = sdk.recognizeIntent(
            "http://sandbox.example:8011/api/v1/intent",
            "派机器狗巡逻A区域",
        )

        assertEquals("security patrol", result.intent)
        assertEquals("patrol", result.scene)
        assertEquals("A", result.area)
        assertEquals("qwen", result.backend)
    }

    @Test
    fun `audio control action uploads transcription through consumer binding`() = runTest {
        initializeSdk()
        runtime.deliverDownlink("COMPUTE_CONNECT_CONFIG", computeConnectConfig("consumer"))

        val action = sdk.createAudioControlAction(
            "css-001",
            AudioControlActionRequest(
                requestId = "voice-action-001",
                audio = byteArrayOf(4, 5, 6),
                fileName = "voice.m4a",
                contentType = "audio/mp4",
            ),
        )

        assertEquals("voice-action-001", action.requestId)
        assertEquals("威吓歹徒", action.text)
        assertEquals("movement", action.intent.intent)
        assertEquals("forward", action.intent.direction)
        assertTrue(action.intent.matched)
        val upload = sandbox.uploads.single()
        assertEquals("http://8.8.8.9:8788/v1/audio-control-actions", upload.url)
        assertEquals("8.8.8.7", upload.sourceIpv4)
        assertEquals("voice-action-001", upload.fields["request_id"])
        assertEquals("voice.m4a", upload.fileName)
    }

    @Test
    fun `capability update calls direct update endpoint`() = runTest {
        initializeSdk(restoreProfile = false)
        val profile = sdk.applyIdentity(
            owner = "Alice",
            name = "AliceAgent",
            description = "AgentModel-X",
            metadata = buildJsonObject {
                put("region", "CN")
                put("os", "Android")
                put("version", "0.14.0")
            },
        )
        sdk.registerCapabilities(
            profile.agentId,
            priority = 1,
            credentials = listOf(buildJsonObject { put("id", "vc-network-a") }),
        )
        runtime.paths.clear()
        val updates = listOf(buildJsonObject {
            put("update_type", "add_skill")
            put("skill_name", "camera")
            put("reference_vc_id", "vc-camera-002")
        })
        val credentials = listOf(buildJsonObject {
            put("id", "vc-camera-002")
            put("claims", buildJsonObject { put("skill_name", "camera") })
        })

        sdk.updateCapabilities(LOCAL_ID, updates, credentials)

        assertEquals("POST", runtime.lastMethod)
        assertEquals("/arf/v1/agent-cards-update", runtime.lastPath)
        assertEquals(LOCAL_ID, runtime.lastBody!!["agent_id"].toString().trim('"'))
        UUID.fromString(runtime.lastBody!!["request_id"].toString().trim('"'))
        assertFalse(runtime.lastBody!!.containsKey("request_type"))
        assertEquals(updates, runtime.lastBody!!["update_items"]!!.jsonArray)
        assertEquals(credentials, runtime.lastBody!!["credentials"]!!.jsonArray)
        assertEquals(AgentLifecycleState.CARD_PUBLISHED, sdk.agentLifecycleState)
        assertEquals(
            listOf("/arf/v1/agent-cards-update"),
            runtime.paths,
        )
    }

    @Test
    fun `registering again replaces identity before publishing the new card`() = runTest {
        initializeSdk(restoreProfile = false)
        val profile = sdk.applyIdentity(
            owner = "Alice",
            name = "AliceAgent",
            description = "AgentModel-X",
            metadata = buildJsonObject {
                put("region", "CN")
                put("os", "Android")
                put("version", "0.15.0")
            },
        )
        sdk.registerCapabilities(
            profile.agentId,
            priority = 1,
            credentials = listOf(buildJsonObject { put("id", "vc-network-a") }),
        )
        runtime.paths.clear()

        sdk.registerCapabilities(
            profile.agentId,
            priority = 2,
            credentials = listOf(buildJsonObject { put("id", "vc-network-b") }),
        )

        assertEquals(
            listOf(
                "/acn-agent/v1/agent-deletions",
                "/idm/v1/identity-applications",
                "/arf/v1/agent-cards",
            ),
            runtime.paths,
        )
        assertEquals(AgentLifecycleState.CARD_PUBLISHED, sdk.agentLifecycleState)
        assertEquals(2, runtime.lastBody!!["priority"]!!.jsonPrimitive.content.toInt())
    }

    @Test
    fun `invalid card replacement is rejected before deregistration`() = runTest {
        initializeSdk(restoreProfile = false)
        val profile = sdk.applyIdentity(
            owner = "Alice",
            name = "AliceAgent",
            description = "AgentModel-X",
            metadata = buildJsonObject {
                put("region", "CN")
                put("os", "Android")
                put("version", "0.15.1")
            },
        )
        sdk.registerCapabilities(
            profile.agentId,
            priority = 1,
            credentials = listOf(buildJsonObject { put("id", "vc-network-a") }),
        )
        runtime.paths.clear()

        val failure = runCatching {
            sdk.registerCapabilities(
                profile.agentId,
                priority = 2,
                credentials = listOf(buildJsonObject {
                    put("id", "vc-external-bound")
                    put("issuer", "did:example:external-issuer")
                    put("claims", buildJsonObject {
                        put("agent_id", profile.agentId)
                        put("skill_name", "camera")
                    })
                }),
            )
        }.exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.CREDENTIAL_EXPIRED, failure.code)
        assertTrue(runtime.paths.isEmpty())
        assertEquals(AgentLifecycleState.CARD_PUBLISHED, sdk.agentLifecycleState)
        assertEquals(profile, sdk.localProfile)
    }

    @Test
    fun `identity application sends exact required HTTP fields`() = runTest {
        initializeSdk(restoreProfile = false)

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
    fun `identity application replaces an identity-ready profile`() = runTest {
        initializeSdk()

        val replacement = sdk.applyIdentity(
            owner = "Alice",
            name = "AliceAgentReplacement",
            description = "AgentModel-X replacement",
            metadata = buildJsonObject {
                put("region", "CN")
                put("os", "Android")
                put("version", "0.14.0")
            },
        )

        assertEquals(LOCAL_ID, replacement.agentId)
        assertEquals(AgentLifecycleState.IDENTITY_READY, sdk.agentLifecycleState)
        assertEquals(replacement, sdk.localProfile)
        assertEquals("/idm/v1/identity-applications", runtime.lastPath)
    }

    @Test
    fun `reset is idempotent in state one without an HTTP request`() = runTest {
        initializeSdk(restoreProfile = false)
        runtime.paths.clear()

        val result = sdk.resetAgent()

        assertTrue(result.success)
        assertEquals("Agent is already in NO_IDENTITY state", result.message)
        assertTrue(runtime.paths.isEmpty())
        assertEquals(AgentLifecycleState.NO_IDENTITY, sdk.agentLifecycleState)
        assertEquals(null, sdk.localProfile)
    }

    @Test
    fun `reset clears a published identity locally without an HTTP request`() = runTest {
        initializeSdk()
        sdk.registerCapabilities(
            LOCAL_ID,
            priority = 1,
            credentials = listOf(buildJsonObject { put("id", "vc-network-a") }),
        )
        runtime.paths.clear()

        val result = sdk.resetAgent()

        assertTrue(result.success)
        assertEquals(
            "Local Agent state reset to NO_IDENTITY; network identity was not changed",
            result.message,
        )
        assertTrue(runtime.paths.isEmpty())
        assertEquals(AgentLifecycleState.NO_IDENTITY, sdk.agentLifecycleState)
        assertEquals(null, sdk.localProfile)
    }

    @Test
    fun `reset clears identity-ready state locally without an HTTP request`() = runTest {
        initializeSdk()
        runtime.paths.clear()

        val result = sdk.resetAgent()

        assertTrue(result.success)
        assertTrue(runtime.paths.isEmpty())
        assertEquals(AgentLifecycleState.NO_IDENTITY, sdk.agentLifecycleState)
        assertEquals(null, sdk.localProfile)
    }

    @Test
    fun `identity application accepts string metadata and normalizes order`() = runTest {
        initializeSdk(restoreProfile = false)

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
        initializeSdk(restoreProfile = false)

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
    fun `invalid identity replacement keeps the current identity`() = runTest {
        initializeSdk()

        val error = runCatching {
            sdk.applyIdentity(
                owner = "Alice",
                name = "AliceAgentReplacement",
                description = "AgentModel-X replacement",
                metadata = buildJsonObject {
                    put("region", "CN")
                    put("os", "Android")
                    put("version", 1)
                },
            )
        }.exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.INVALID_ARGUMENT, error.code)
        assertEquals(AgentLifecycleState.IDENTITY_READY, sdk.agentLifecycleState)
        assertEquals(LOCAL_ID, sdk.localProfile?.agentId)
        assertEquals("", runtime.lastPath)
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
    fun `create group response preserves an earlier active group config`() = runTest {
        initializeSdk()
        assertEquals(NetworkMessageAction.ACK, runtime.deliverGroupConfig(groupConfig()))

        val group = sdk.createGroup(
            agentId = LOCAL_ID,
            targetAgentIds = listOf(PEER_ID),
            groupName = "patrol-group",
            dnn = "internet",
            maxMembers = 2,
        )
        val status = sdk.createComputingSession(createComputeRequest())

        assertEquals("ACTIVE", group.status)
        assertEquals("css-001", status.computeServiceSessionId)
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

    private suspend fun initializeSdk(
        restoreProfile: Boolean = true,
    ) {
        val result = sdk.initialize(
            agentRuntimeIp = "192.168.3.10",
            agentRuntimePort = 8080,
            localTcpPort = 4001,
            localUdpPort = 28443,
            masqueServerUrl = "https://192.168.3.10:4433",
        )
        assertEquals("8.8.8.7:4001", result.agentTcpEndpoint)
        assertEquals("192.168.1.10", result.masqueOuterSourceIp)
        assertEquals("8.8.8.7/32", result.agentTunCidr)
        assertEquals("8.8.8.7/32", tunnel.establishedConfiguration?.agentTunCidr)
        assertTrue(tunnel.establishedConfiguration?.routes?.isEmpty() == true)
        assertEquals(tunnel.clientIdentityDirectory, masque.configuration?.identityDirectory)
        if (restoreProfile) {
            sdk.restoreLocalProfile(
                AgentProfile(LOCAL_ID, "Agent A", buildJsonObject { put("id", "vc-a") }),
            )
        }
    }

    private fun createComputeRequest(cpuMillicores: Long = 2_000): ComputeSessionRequest =
        ComputeSessionRequest(
            messageType = "COMPUTE_SESSION_REQUEST",
            requestType = ComputeRequestType.CREATE,
            inputFormat = ComputeInputFormat.STRUCTURED,
            requestId = "create-001",
            acnContext = AcnContext("g1", LOCAL_ID, PEER_ID),
            constraints = ComputeConstraints(
                capabilityId = "dog-vision",
                resources = ComputeResources(
                    cpuMillicores = cpuMillicores,
                    memoryMib = 4_096,
                ),
                allowBaseQos = true,
            ),
            uiLocale = "zh-CN",
        )

    private fun computeConnectConfig(role: String): JsonObject = buildJsonObject {
        put("compute_service_session_id", "css-001")
        put("compute_instance_id", "ci-001")
        put("binding_ref", "binding-css-001")
        put("role", role)
        put("receiver_agent_id", LOCAL_ID)
        put("service_endpoint", "http://8.8.8.9:8788")
        put("network_binding", buildJsonObject {
            put("pdu_session_id", 1)
            put("dnn", "internet")
            put("snssai", buildJsonObject {
                put("sst", 1)
                put("sd", "010203")
            })
            put("ue_ipv4", "8.8.8.7")
            put("runtime_data_plane", buildJsonObject {
                put("access_type", "HTTP3_CONNECT_IP")
                put("session_selection", "EXACT_PDU_SESSION_ID")
            })
        })
        put("connection_parameters", buildJsonObject {
            put("media_connections_path", "/v1/media-connections")
            put("transport", "WEBRTC")
            put(
                "recognition_target_path_template",
                "/v1/recognition-targets/{compute_service_session_id}",
            )
        })
    }

    private fun computeStatus(status: String, revision: String): JsonObject = buildJsonObject {
        put("request_id", "create-001")
        put("compute_service_session_id", "css-001")
        put("status_revision", revision)
        put("status", status)
        put("cause", if (status == "FAILED") "resource-activation-failed" else "")
    }

    private fun computeSessionClose(role: String): JsonObject = buildJsonObject {
        put("compute_service_session_id", "css-001")
        put("compute_instance_id", "ci-001")
        put("binding_ref", "binding-css-001")
        put("role", role)
        put("receiver_agent_id", LOCAL_ID)
        put("cause", "released")
    }

    private fun groupConfig(
        peerPort: String = "4001",
        includeSecondPeer: Boolean = false,
        timestamp: Instant = Instant.now(),
        peerIp: String = "8.8.8.8",
        peerSupi: String? = "imsi-001010000000002",
    ): JsonObject = buildJsonObject {
        put("notification_type", "acf_group_config")
        put("version", "1.0.0")
        put("timestamp", timestamp.toString())
        put("group_id", "g1")
        put("members", buildJsonObject {
            put(
                "agent1",
                member(LOCAL_ID, "Agent A", "imsi-001010000000001", "8.8.8.7", "4001"),
            )
            put("not-an-id", member(PEER_ID, "Agent B", peerSupi, peerIp, peerPort))
            if (includeSecondPeer) {
                put(
                    "agent-c",
                    member(
                        SECOND_PEER_ID,
                        "Agent C",
                        "imsi-001010000000003",
                        "8.8.8.10",
                        "4002",
                    ),
                )
            }
        })
        put("proof", buildJsonObject { put("jws", "test") })
    }

    private fun member(
        id: String,
        name: String,
        supi: String?,
        ip: String,
        tcpPort: String,
    ): JsonObject = buildJsonObject {
        put("agent_id", id)
        put("agent_name", name)
        supi?.let { put("supi", it) }
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
        val replacements = mutableListOf<Pair<String, Set<String>>>()
        var establishedConfiguration: TunnelConfiguration? = null
        var computeReplacementGate: CompletableDeferred<Unit>? = null
        val computeReplacementStarted = CompletableDeferred<Unit>()
        private var swapper: (suspend (Int) -> Unit)? = null
        private var replacedListener: (suspend () -> Unit)? = null

        override suspend fun establish(configuration: TunnelConfiguration) {
            establishedConfiguration = configuration
        }
        override suspend fun replaceGroupPeers(groupId: String, peerIps: Set<String>) {
            if (
                groupId.startsWith("computing:") && peerIps.isNotEmpty() &&
                computeReplacementGate != null
            ) {
                computeReplacementStarted.complete(Unit)
                computeReplacementGate!!.await()
            }
            replacements += groupId to peerIps.toSet()
            if (peerIps.isEmpty()) groupPeers.remove(groupId) else groupPeers[groupId] = peerIps
        }
        override fun currentAllowedPeerIps(): Set<String> = groupPeers.values.flatten().toSet()
        override fun setTunFdSwapper(swapper: suspend (Int) -> Unit) { this.swapper = swapper }
        override fun setTunReplacedListener(listener: suspend () -> Unit) {
            replacedListener = listener
        }
        suspend fun simulateTunReplacement(newFd: Int) {
            checkNotNull(swapper) { "TUN fd swapper is not registered" }.invoke(newFd)
            replacedListener?.invoke()
        }
        override suspend fun close() = Unit
    }

    private class FakeMasque(
        private val events: MutableList<String>? = null,
    ) : MasqueTransport {
        override var connected: Boolean = false
        var configuration: MasqueConfiguration? = null
        override suspend fun start(tunFd: Int, configuration: MasqueConfiguration) {
            this.configuration = configuration
            connected = true
        }
        override suspend fun replaceTunFd(tunFd: Int) {
            events?.add("masque-replace-$tunFd")
        }
        override suspend fun close() { connected = false }
    }

    private class FakeRuntime : RuntimeTransport {
        var lastMethod = ""
        var lastPath = ""
        var lastBody: JsonObject? = null
        val paths = mutableListOf<String>()
        val bodies = mutableMapOf<String, JsonObject>()
        var ueInfoRequests = 0
        var computeRequestHandler: (suspend (JsonObject) -> RuntimeHttpResponse)? = null
        var downlinkHandler: (suspend (String, Int, JsonObject) -> JsonObject?)? = null

        override suspend fun getUeInfo(): JsonObject {
            ueInfoRequests += 1
            return buildJsonObject {
                put("identity", buildJsonObject { put("supi", "imsi-001010000000001") })
                put("nas", buildJsonObject {
                    put("state", "session_ready")
                    put("registered", true)
                    put("security_context", true)
                })
                put("pdu_sessions", buildJsonArray {
                    add(buildJsonObject {
                        put("pdu_session_id", 1)
                        put("state", "active")
                        put("type", "IPv4")
                        put("dnn", "internet")
                        put("snssai", buildJsonObject {
                            put("sst", 1)
                            put("sd", "010203")
                        })
                        put("ipv4", "8.8.8.7")
                        put("default_route", true)
                    })
                })
                put("data_plane_accesses", buildJsonArray {
                    add(buildJsonObject {
                        put("access_type", "HTTP3_CONNECT_IP")
                        put(
                            "endpoint_template",
                            "https://runtime.example/masque/{pdu_session_id}",
                        )
                        put("session_selection", "EXACT_PDU_SESSION_ID")
                    })
                })
            }
        }

        override suspend fun getAcnStatus(): JsonObject =
            buildJsonObject { put("ready", true) }

        override suspend fun startDownlink(
            onReconnected: suspend () -> Unit,
            handler: suspend (String, Int, JsonObject) -> JsonObject?,
        ) { downlinkHandler = handler }
        suspend fun deliverDownlink(
            messageType: String,
            payload: JsonObject,
            transactionId: Int = 49,
        ): JsonObject? = downlinkHandler!!(messageType, transactionId, payload)
        suspend fun deliverGroupConfig(payload: JsonObject): NetworkMessageAction {
            val response = checkNotNull(
                deliverDownlink("ACN_AGENT_GROUPING_NOTIFICATION", payload),
            )
            return NetworkMessageAction.valueOf(response["result"]!!.jsonPrimitive.content)
        }
        override suspend fun request(method: String, path: String, body: JsonObject): JsonObject {
            lastMethod = method
            lastPath = path
            paths += path
            lastBody = body
            bodies[path] = body
            return if (path == "/v1/computing/session-requests") {
                buildJsonObject {
                    put("message_type", "COMPUTE_SESSION_STATUS")
                    put("request_id", body["request_id"]!!)
                    put("compute_service_session_id", "css-001")
                    put("status_revision", "1")
                    put("status", "ACCEPTED")
                    put("cause", "")
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
        override suspend fun requestWithStatus(
            method: String,
            path: String,
            body: JsonObject,
        ): RuntimeHttpResponse {
            if (path == "/v1/computing/session-requests" && computeRequestHandler != null) {
                lastMethod = method
                lastPath = path
                paths += path
                lastBody = body
                bodies[path] = body
                return checkNotNull(computeRequestHandler).invoke(body)
            }
            return RuntimeHttpResponse(
                if (
                    path == "/v1/computing/session-requests" &&
                    body["request_type"]?.jsonPrimitive?.content == "CREATE"
                ) 202 else 200,
                request(method, path, body),
            )
        }
        override suspend fun close() = Unit
    }

    private class FakeServer(
        private val onStart: () -> Unit = {},
        private val onClose: () -> Unit = {},
    ) : LocalServer {
        var agentIp = ""
        var startCount = 0
        var closeCount = 0
        override suspend fun start(
            agentIp: String,
            tcpPort: Int,
            udpPort: Int,
            onA2aMessage: suspend (JsonObject) -> Unit,
        ) {
            this.agentIp = agentIp
            startCount += 1
            onStart()
        }
        override suspend fun close() {
            closeCount += 1
            onClose()
        }
    }

    private class FakeLocalAddressResolver : LocalAddressResolver {
        var calls = 0
        override fun resolve(serverUri: java.net.URI): String {
            calls += 1
            return "192.168.1.10"
        }
    }

    private class FakePeer : PeerMessenger {
        var called = false
        var ip = ""
        var port = 0
        var body: JsonObject? = null
        val bodies = mutableListOf<JsonObject>()
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
            bodies += body
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
        var cameraId = ""
        var prepareCount = 0

        override fun supportsVideoCodec(codec: String): Boolean =
            codec.uppercase() in setOf("H264", "VP8")

        override suspend fun prepareVideoUpload(
            session: ComputingSession,
            cameraId: String,
            width: Int,
            height: Int,
            fps: Int,
            bitrateKbps: Int,
            timeoutSeconds: Double,
        ): PreparedMediaConnection<VideoUploadHandle> {
            this.cameraId = cameraId
            prepareCount += 1
            val upload = object : VideoUploadHandle {
                override val trackId = "camera-track-1"
                override var state = "RUNNING"
                override suspend fun pause() { state = "PAUSED" }
                override suspend fun resume() { state = "RUNNING" }
                override suspend fun stop() { state = "STOPPED" }
            }
            return FakePrepared("producer", upload)
        }

        override suspend fun prepareProcessedVideo(
            session: ComputingSession,
            timeoutSeconds: Double,
        ): PreparedMediaConnection<LocalProcessedVideo> {
            val local = object : LocalProcessedVideo {
                override val track = object : VideoTrack {
                    override val trackId = "processed-track-1"
                    override fun addSink(sink: Any) = Unit
                    override fun removeSink(sink: Any) = Unit
                }
                override suspend fun close() = Unit
            }
            return FakePrepared("consumer", local)
        }

        override suspend fun close() = Unit
    }

    private class FakePrepared<T>(role: String, private val result: T) :
        PreparedMediaConnection<T> {
        override val offerSdp =
            "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n" +
                "a=${if (role == "producer") "sendonly" else "recvonly"}\r\n" +
                "a=candidate:1 1 UDP 1 8.8.8.7 50000 typ host\r\n" +
                "a=end-of-candidates\r\n"
        override suspend fun applyAnswer(answerSdp: String, timeoutSeconds: Double): T = result
        override suspend fun abort() = Unit
    }

    private class FakeSandbox : SandboxTransport {
        data class Upload(
            val url: String,
            val fields: Map<String, String>,
            val fileName: String,
            val sourceIpv4: String?,
        )

        val requests = mutableListOf<Triple<String, String, JsonObject?>>()
        val requestSourceIpv4 = mutableListOf<String?>()
        val uploads = mutableListOf<Upload>()
        var failuresRemaining = 0
        var recognitionTarget: JsonObject? = null
        var controlAction: JsonObject? = null
        var intentResponse = buildJsonObject {
            put("status", "success")
            put("executor", "robot dog")
            put("intent", "patrol")
            put("scene", "patrol")
            put("argument", "A区域")
            put("normalized_argument", "A区域")
            put("confidence", 1.0)
            put("backend", "rules")
        }

        override suspend fun requestWithStatus(
            method: String,
            url: String,
            body: JsonObject?,
            timeoutSeconds: Double,
            sourceIpv4: String?,
        ): RuntimeHttpResponse {
            requests += Triple(method, url, body)
            requestSourceIpv4 += sourceIpv4
            if (url.endsWith("/api/v1/intent")) {
                return RuntimeHttpResponse(200, intentResponse)
            }
            if (method == "DELETE") return RuntimeHttpResponse(204, JsonObject(emptyMap()))
            if ("/v1/recognition-targets/" in url) {
                if (method == "PUT") {
                    val request = checkNotNull(body)
                    recognitionTarget = buildJsonObject {
                        put("request_id", request["request_id"]!!)
                        put("computing_context", request["computing_context"]!!)
                        put("status", "APPLIED")
                        put("target_revision", "1")
                        put("target", buildJsonObject {
                            put("label", "红色玩偶")
                            put("prompt", "red toy")
                        })
                    }
                    return RuntimeHttpResponse(200, checkNotNull(recognitionTarget))
                }
                if (method == "GET" && recognitionTarget != null) {
                    return RuntimeHttpResponse(200, checkNotNull(recognitionTarget))
                }
                return RuntimeHttpResponse(404, buildJsonObject {
                    put("error", buildJsonObject {
                        put("code", "recognition-target-not-set")
                        put("message", "not set")
                    })
                })
            }
            if ("/v1/control-actions" in url) {
                if (method == "POST") {
                    val request = checkNotNull(body)
                    val input = request["input"]!!.jsonObject
                    val isText = input["type"]!!.jsonPrimitive.content == "TEXT"
                    controlAction = buildJsonObject {
                        put("request_id", request["request_id"]!!)
                        put("action_id", "action-001")
                        put("computing_context", request["computing_context"]!!)
                        put(
                            "normalized_action",
                            if (isText) "search_object"
                            else request["action"]!!.jsonPrimitive.content,
                        )
                        put(
                            "normalized_parameters",
                            if (isText) buildJsonObject { put("query", "cup") }
                            else request["parameters"]!!.jsonObject,
                        )
                        put("status", "RUNNING")
                        put("result", buildJsonObject { put("phase", "started") })
                        put("cause", "")
                    }
                    return RuntimeHttpResponse(202, checkNotNull(controlAction))
                }
                if (method == "GET" && controlAction != null) {
                    val result = checkNotNull(controlAction).toMutableMap()
                    result.remove("computing_context")
                    result["status"] = JsonPrimitive("COMPLETED")
                    return RuntimeHttpResponse(200, JsonObject(result))
                }
                return RuntimeHttpResponse(404, buildJsonObject {
                    put("error", buildJsonObject {
                        put("code", "action-not-found")
                        put("message", "not found")
                    })
                })
            }
            if (failuresRemaining > 0) {
                failuresRemaining -= 1
                throw AgentSdkException(
                    ErrorCode.TIMEOUT,
                    "unknown POST result",
                    retryable = true,
                )
            }
            val request = checkNotNull(body)
            val context = request["computing_context"]!!.jsonObject
            val role = context["role"]!!.jsonPrimitive.content
            return RuntimeHttpResponse(201, buildJsonObject {
                put("request_id", request["request_id"]!!)
                put("computing_context", context)
                put("media_connection_id", "media-$role-001")
                put("answer", buildJsonObject {
                    put("type", "answer")
                    put(
                        "sdp",
                        "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n" +
                            "a=${if (role == "producer") "recvonly" else "sendonly"}\r\n" +
                            "a=candidate:2 1 UDP 1 8.8.8.10 51000 typ host\r\n",
                    )
                })
            })
        }

        override suspend fun uploadWithStatus(
            url: String,
            fields: Map<String, String>,
            fileFieldName: String,
            fileName: String,
            contentType: String,
            content: ByteArray,
            timeoutSeconds: Double,
            sourceIpv4: String?,
        ): RuntimeHttpResponse {
            uploads += Upload(url, fields, fileName, sourceIpv4)
            if (url.endsWith("/api/v1/transcribe")) {
                return RuntimeHttpResponse(200, buildJsonObject {
                    put("request_id", fields.getValue("request_id"))
                    put("text", "派机器狗巡逻A区域")
                    put("intent", buildJsonObject {
                        put("type", "TASK")
                        put("parameters", buildJsonObject { put("area", "A") })
                    })
                    put("required_skills", buildJsonArray {
                        add("patrol")
                        add("camera")
                    })
                })
            }
            return RuntimeHttpResponse(200, buildJsonObject {
                put("request_id", fields.getValue("request_id"))
                put("text", "威吓歹徒")
                put("intent", buildJsonObject {
                    put("executor", "robot dog")
                    put("intent", "movement")
                    put("direction", "forward")
                    put("matched", true)
                    put("backend", "rules")
                })
            })
        }

        override suspend fun close() = Unit
    }

    private companion object {
        const val LOCAL_ID = "did:example:agent-a"
        const val PEER_ID = "did:example:agent-b"
        const val SECOND_PEER_ID = "did:example:agent-c"
    }
}
