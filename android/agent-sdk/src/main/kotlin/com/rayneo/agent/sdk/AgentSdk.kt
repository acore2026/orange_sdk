package com.rayneo.agent.sdk

import com.rayneo.agent.sdk.group.GroupMemberCache
import com.rayneo.agent.sdk.masque.NativeMasqueTransport
import com.rayneo.agent.sdk.masque.NativeMasqueBridge
import com.rayneo.agent.sdk.model.AgentProfile
import com.rayneo.agent.sdk.model.DiscoveredAgent
import com.rayneo.agent.sdk.model.GroupConfigSnapshot
import com.rayneo.agent.sdk.model.GroupInfo
import com.rayneo.agent.sdk.model.MessageReceipt
import com.rayneo.agent.sdk.model.NetworkAbility
import com.rayneo.agent.sdk.model.NetworkMessageAction
import com.rayneo.agent.sdk.model.NetworkMessageType
import com.rayneo.agent.sdk.model.OffloadingSession
import com.rayneo.agent.sdk.model.OperationResult
import com.rayneo.agent.sdk.model.SdkInitResult
import com.rayneo.agent.sdk.security.RejectUnconfiguredMessageSigner
import com.rayneo.agent.sdk.security.RejectUnconfiguredMessageSignatureVerifier
import com.rayneo.agent.sdk.security.RejectUnconfiguredProofVerifier
import com.rayneo.agent.sdk.server.TcpJsonLocalServer
import com.rayneo.agent.sdk.transport.GroupMessageListener
import com.rayneo.agent.sdk.transport.LocalServer
import com.rayneo.agent.sdk.transport.MediaOffloadAdapter
import com.rayneo.agent.sdk.transport.MasqueConfiguration
import com.rayneo.agent.sdk.transport.MasqueTransport
import com.rayneo.agent.sdk.transport.MessageSignatureVerifier
import com.rayneo.agent.sdk.transport.MessageSigner
import com.rayneo.agent.sdk.transport.NetworkMessageListener
import com.rayneo.agent.sdk.transport.OkHttpPeerMessenger
import com.rayneo.agent.sdk.transport.OkHttpRuntimeTransport
import com.rayneo.agent.sdk.transport.PeerMessenger
import com.rayneo.agent.sdk.transport.ProofVerifier
import com.rayneo.agent.sdk.transport.RuntimeTransport
import com.rayneo.agent.sdk.transport.TunnelConfiguration
import com.rayneo.agent.sdk.transport.TunnelController
import com.rayneo.agent.sdk.transport.VideoTrack
import com.rayneo.agent.sdk.transport.VideoUploadHandle
import com.rayneo.agent.sdk.vpn.AgentVpnService
import com.rayneo.agent.sdk.vpn.VpnTunnelController
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.coroutines.withTimeout
import java.net.URI
import java.time.Instant
import java.util.UUID

class AgentSdk(
    private val tunnelController: TunnelController,
    private val masqueTransport: MasqueTransport,
    private val proofVerifier: ProofVerifier = RejectUnconfiguredProofVerifier(),
    private val messageSigner: MessageSigner = RejectUnconfiguredMessageSigner,
    private val messageSignatureVerifier: MessageSignatureVerifier = RejectUnconfiguredMessageSignatureVerifier,
    private val peerMessenger: PeerMessenger = OkHttpPeerMessenger(),
    private val runtimeFactory: (String, Int) -> RuntimeTransport = { host, port ->
        OkHttpRuntimeTransport(host, port)
    },
    private val localServerFactory: () -> LocalServer = { TcpJsonLocalServer() },
    private val mediaOffloadAdapter: MediaOffloadAdapter? = null,
) {
    private enum class State { NEW, INITIALIZING, READY, CLOSING, CLOSED }

    private var state = State.NEW
    private var runtime: RuntimeTransport? = null
    private var localServer: LocalServer? = null
    private var groupCache: GroupMemberCache? = null
    private var networkListener: NetworkMessageListener? = null
    private var groupListener: GroupMessageListener? = null
    private var profile: AgentProfile? = null
    private var agentTunIp: String = ""
    private var agentTunCidr: String = ""
    private var localTcpPort: Int = 0
    private var localUdpPort: Int = 0
    private val groups = mutableMapOf<String, GroupInfo>()
    private val offloadingSessions = mutableMapOf<String, OffloadingSession>()

    suspend fun initialize(
        agentRuntimeIp: String,
        agentRuntimePort: Int,
        localVlanIp: String,
        localTcpPort: Int,
        localUdpPort: Int,
        masqueServerUrl: String,
        masqueAuthorization: String? = null,
        tunMtu: Int = 1280,
    ): SdkInitResult {
        if (state != State.NEW && state != State.CLOSED) {
            throw AgentSdkException(ErrorCode.INVALID_ARGUMENT, "SDK is already initialized")
        }
        validatePort(agentRuntimePort, "agentRuntimePort")
        validatePort(localTcpPort, "localTcpPort")
        validatePort(localUdpPort, "localUdpPort")
        val uri = try {
            URI(masqueServerUrl)
        } catch (error: Exception) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "Invalid MASQUE URL",
                "masqueServerUrl",
                cause = error,
            )
        }
        if (uri.scheme != "https" || uri.host.isNullOrBlank()) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "masqueServerUrl must be an https URL",
                "masqueServerUrl",
            )
        }
        this.localTcpPort = localTcpPort
        this.localUdpPort = localUdpPort
        state = State.INITIALIZING
        try {
            runtime = runtimeFactory(agentRuntimeIp, agentRuntimePort).also { transport ->
                transport.connect()
            }
            val registration = runtime!!.registerEndpoint(
                localVlanIp, localTcpPort, localUdpPort
            )
            this.agentTunCidr = registration.agentTunCidr
            this.agentTunIp = registration.ueIp
            tunnelController.establish(
                TunnelConfiguration(agentTunCidr, emptySet(), tunMtu)
            )
            groupCache = GroupMemberCache(tunnelController)
            localServer = localServerFactory().also { server ->
                server.start(
                    physicalIp = localVlanIp,
                    agentIp = agentTunIp,
                    tcpPort = localTcpPort,
                    udpPort = localUdpPort,
                    onA2aMessage = ::handleA2aMessage,
                )
            }
            masqueTransport.start(
                tunnelController.tunFd,
                MasqueConfiguration(
                    serverUrl = masqueServerUrl,
                    authorization = masqueAuthorization,
                    localVlanIp = localVlanIp,
                    agentTunCidr = agentTunCidr,
                    mtu = tunMtu,
                    identityDirectory = tunnelController.clientIdentityDirectory,
                ),
            )
            tunnelController.setTunFdSwapper(masqueTransport::replaceTunFd)
            state = State.READY
            runtime!!.startDownlink(::handleRuntimeDownlink)
            return SdkInitResult(
                runtimeConnected = true,
                masqueConnected = masqueTransport.connected,
                localTcpEndpoint = "$localVlanIp:$localTcpPort",
                localUdpEndpoint = "$localVlanIp:$localUdpPort",
                agentTcpEndpoint = "$agentTunIp:$localTcpPort",
                agentUdpEndpoint = "$agentTunIp:$localUdpPort",
                agentTunCidr = agentTunCidr,
                masqueProxyEndpoint = masqueServerUrl,
            )
        } catch (error: Exception) {
            close()
            throw error
        }
    }

    fun registerNetworkMessageListener(listener: NetworkMessageListener): AutoCloseable {
        if (networkListener != null) {
            throw AgentSdkException(
                ErrorCode.LISTENER_ALREADY_REGISTERED,
                "Network message listener is already registered",
            )
        }
        networkListener = listener
        return AutoCloseable { if (networkListener === listener) networkListener = null }
    }

    fun registerGroupMessageListener(listener: GroupMessageListener): AutoCloseable {
        groupListener = listener
        return AutoCloseable { if (groupListener === listener) groupListener = null }
    }

    suspend fun handleGroupConfig(payload: JsonObject): NetworkMessageAction {
        requireReady()
        val localProfile = profile ?: return NetworkMessageAction.REJECT
        proofVerifier.verifyGroupConfig(payload)
        val cache = groupCache ?: throw AgentSdkException(
            ErrorCode.SDK_NOT_INITIALIZED,
            "Group cache is unavailable",
        )
        val candidate = cache.buildCandidate(
            payload,
            localAgentId = localProfile.agentId,
            localAgentIp = agentTunIp,
            localTcpPort = localTcpPort,
            localUdpPort = localUdpPort,
        )
        cache.commit(candidate, localProfile.agentId)
        groups.getOrPut(candidate.groupId) { GroupInfo(candidate.groupId, candidate.groupId) }
            .status = "ACTIVE"
        try {
            networkListener?.onNetworkMessage(NetworkMessageType.GROUP_CONFIG, payload)
        } catch (_: Exception) {
            // The verified snapshot is already committed; application notification
            // cannot roll back cache and route state.
        }
        return NetworkMessageAction.ACK
    }

    private suspend fun handleGroupInvitation(payload: JsonObject): NetworkMessageAction {
        val listener = networkListener ?: return NetworkMessageAction.REJECT
        return listener.onNetworkMessage(NetworkMessageType.GROUP_INVITATION, payload)
    }

    private suspend fun handleRuntimeDownlink(
        messageType: String,
        transactionId: Int,
        payload: JsonObject,
    ): NetworkMessageAction {
        if (transactionId < 0) return NetworkMessageAction.REJECT
        return when {
            messageType == "ACN_AGENT_GROUPING_INVITATION" ->
                handleGroupInvitation(payload)
            messageType == "ACN_AGENT_GROUP_CONFIG" ||
                messageType == "ACF_GROUP_CONFIG" ||
                payload["notification_type"]?.jsonPrimitive?.contentOrNull ==
                "acf_group_config" -> handleGroupConfig(payload)
            else -> networkListener?.onNetworkMessage(
                NetworkMessageType.UNKNOWN,
                payload,
            ) ?: NetworkMessageAction.REJECT
        }
    }

    suspend fun handleA2aMessage(payload: JsonObject) {
        requireReady()
        val localProfile = profile ?: throw AgentSdkException(
            ErrorCode.GROUP_NOT_ACTIVE,
            "Local identity is unavailable",
        )
        val listener = groupListener ?: throw AgentSdkException(
            ErrorCode.GROUP_NOT_ACTIVE,
            "Group message listener is unavailable",
        )
        val groupId = payload.requireString("group_id")
        val senderId = payload.requireString("sender_agent_id")
        if (payload.requireString("target_agent_id") != localProfile.agentId) {
            throw AgentSdkException(
                ErrorCode.TARGET_NOT_IN_GROUP,
                "A2A message targets another agent",
            )
        }
        val sender = groupCache!!.resolve(groupId, senderId)
        messageSignatureVerifier.verifyA2a(payload, sender.didKey)
        val userPayload = payload["payload"] as? JsonObject ?: throw AgentSdkException(
            ErrorCode.INVALID_ARGUMENT,
            "A2A payload must be a JSON object",
        )
        listener.onGroupMessage(groupId, senderId, userPayload)
    }

    suspend fun sendMessage(
        groupId: String,
        targetAgentId: String,
        jsonMessage: JsonObject,
        timeoutSeconds: Double = 5.0,
    ): MessageReceipt {
        requireReady()
        if (timeoutSeconds <= 0.0) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "timeoutSeconds must be greater than zero",
                "timeoutSeconds",
            )
        }
        val localProfile = profile ?: throw AgentSdkException(
            ErrorCode.GROUP_NOT_ACTIVE,
            "Local identity is unavailable",
        )
        val target = groupCache!!.resolve(groupId, targetAgentId)
        val messageId = UUID.randomUUID().toString()
        val unsigned = buildJsonObject {
            put("message_id", messageId)
            put("group_id", groupId)
            put("sender_agent_id", localProfile.agentId)
            put("target_agent_id", targetAgentId)
            put("timestamp", Instant.now().toString())
            put("payload", jsonMessage)
        }
        val body = buildJsonObject {
            unsigned.forEach { (key, value) -> put(key, value) }
            put("proof", messageSigner.signA2a(unsigned))
        }
        val response = peerMessenger.send(
            target.agentIp,
            target.tcpPort,
            body,
            (timeoutSeconds * 1000).toLong(),
        )
        val delivered = response["ack"]?.jsonPrimitive?.booleanOrNull == true
        return MessageReceipt(messageId, delivered, if (delivered) Instant.now() else null)
    }

    suspend fun applyIdentity(
        owner: String,
        name: String,
        publicKey: String,
        description: String = "",
        metadata: JsonObject = buildJsonObject { },
    ): AgentProfile {
        requireReady()
        val response = runtime!!.request("POST", "/idm/v1/identity-applications", buildJsonObject {
            put("owner", owner)
            put("name", name)
            put("public_key", publicKey)
            put("description", description)
            put("metadata", metadata)
        })
        return AgentProfile(
            agentId = response.requireString("agent_id"),
            agentName = response["agent_name"]?.jsonPrimitive?.contentOrNull ?: name,
            identityVc = response["identity_vc"] as? JsonObject ?: buildJsonObject { },
        ).also { profile = it }
    }

    fun restoreLocalProfile(restored: AgentProfile) {
        profile = restored
    }

    suspend fun deregisterIdentity(agentId: String, reason: String = ""): OperationResult =
        operation("POST", "/acn-agent/v1/agent-deletions", buildJsonObject {
            put("agent_id", agentId)
            put("reason", reason)
        }).also { if (profile?.agentId == agentId) profile = null }

    suspend fun getNetworkAbility(agentId: String, intent: String = ""): NetworkAbility {
        requireReady()
        val response = runtime!!.request("POST", "/idm/v1/network-ability", buildJsonObject {
            put("agent_id", agentId)
            put("intent", intent)
        })
        val abilities = (response["abilities"] as? JsonArray)
            ?.mapNotNull { it.jsonPrimitive.contentOrNull }
            ?: emptyList()
        return NetworkAbility(
            response["ability_vc"] as? JsonObject ?: buildJsonObject { },
            abilities,
            response["valid_until"]?.jsonPrimitive?.contentOrNull?.let(Instant::parse),
        )
    }

    suspend fun registerCapabilities(
        agentId: String,
        priority: Int,
        credentials: List<JsonObject>,
    ): OperationResult = operation("POST", "/arf/v1/agent-cards", buildJsonObject {
        put("agent_id", agentId)
        put("priority", priority)
        put("vc_list", JsonArray(credentials))
    })

    suspend fun updateCapabilities(
        agentId: String,
        updateItems: List<JsonObject>,
        credentials: List<JsonObject>,
    ): OperationResult = operation("POST", "/arf/v1/agent-cards-update", buildJsonObject {
        put("agent_id", agentId)
        put("update_items", JsonArray(updateItems))
        put("credentials", JsonArray(credentials))
    })

    suspend fun discoverAgents(
        taskId: String,
        agentId: String,
        taskDescription: String,
        requiredSkills: List<String>,
        discoveryScope: String = "intra_plmn",
        maxResults: Int = 10,
    ): List<DiscoveredAgent> {
        requireReady()
        val response = runtime!!.request("POST", "/arf/v1/agent-discoveries", buildJsonObject {
            put("task_id", taskId)
            put("agent_id", agentId)
            put("task_description", taskDescription)
            put("required_skills", buildJsonArray { requiredSkills.forEach { add(JsonPrimitive(it)) } })
            put("discovery_scope", discoveryScope)
            put("max_results", maxResults)
        })
        return (response["agents"] as? JsonArray).orEmpty().map { element ->
            val item = element.jsonObject
            DiscoveredAgent(
                agentId = item.requireString("agent_id"),
                ip = item["ip"]?.jsonPrimitive?.contentOrNull ?: "",
                tcpPort = item["tcp_port"]?.jsonPrimitive?.intOrNull ?: 0,
                udpPort = item["udp_port"]?.jsonPrimitive?.intOrNull ?: 0,
                skills = (item["skills"] as? JsonArray).orEmpty().map { it.jsonPrimitive.content },
                priority = item["priority"]?.jsonPrimitive?.intOrNull ?: 0,
            )
        }.sortedBy { it.priority }
    }

    suspend fun createGroup(
        agentId: String,
        targetAgentIds: List<String>,
        groupName: String,
        scope: String = "private",
        maxMembers: Int = 10,
    ): GroupInfo {
        requireReady()
        val response = runtime!!.request("POST", "/acf/v1/agents-grouping", buildJsonObject {
            put("agent_id", agentId)
            put("target_agent_ids", buildJsonArray { targetAgentIds.forEach { add(JsonPrimitive(it)) } })
            put("group_name", groupName)
            put("scope", scope)
            put("max_members", maxMembers)
        })
        return GroupInfo(response.requireString("group_id"), groupName).also {
            groups[it.groupId] = it
        }
    }

    suspend fun createOffloadingSession(
        agentId: String,
        taskType: String,
        sandboxId: String? = null,
        timeoutSeconds: Double = 30.0,
    ): OffloadingSession {
        requireReady()
        if (timeoutSeconds <= 0.0) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "timeoutSeconds must be greater than zero",
                "timeoutSeconds",
            )
        }
        val response = runtime!!.request("POST", "/compute/v1/offloading-sessions", buildJsonObject {
            put("agent_id", agentId)
            put("task_type", taskType)
            sandboxId?.let { put("preferred_sandbox_id", it) }
        })
        var session = OffloadingSession(
            response.requireString("session_id"),
            response["sandbox_id"]?.jsonPrimitive?.contentOrNull ?: "",
            response["state"]?.jsonPrimitive?.contentOrNull ?: "CONNECTING",
            response["expires_at"]?.jsonPrimitive?.contentOrNull?.let(Instant::parse),
            response,
        )
        mediaOffloadAdapter?.let { adapter ->
            withTimeout((timeoutSeconds * 1000).toLong()) {
                adapter.connect(session, response, timeoutSeconds)
            }
            session = session.copy(state = "CONNECTED")
        }
        offloadingSessions[session.sessionId] = session
        return session
    }

    suspend fun startVideoUpload(
        sessionId: String,
        cameraId: String = "0",
        width: Int = 1920,
        height: Int = 1080,
        fps: Int = 30,
        bitrateKbps: Int = 4000,
    ): VideoUploadHandle {
        val session = requireOffloadingSession(sessionId)
        listOf(
            "width" to width,
            "height" to height,
            "fps" to fps,
            "bitrateKbps" to bitrateKbps,
        ).firstOrNull { it.second <= 0 }?.let { (field, _) ->
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "$field must be greater than zero",
                field,
            )
        }
        return requireMediaAdapter().startVideoUpload(
            session,
            cameraId,
            width,
            height,
            fps,
            bitrateKbps,
        )
    }

    suspend fun getProcessedVideoStream(
        sessionId: String,
        timeoutSeconds: Double = 10.0,
    ): VideoTrack {
        if (timeoutSeconds <= 0.0) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "timeoutSeconds must be greater than zero",
                "timeoutSeconds",
            )
        }
        val session = requireOffloadingSession(sessionId)
        return withTimeout((timeoutSeconds * 1000).toLong()) {
            requireMediaAdapter().getProcessedVideoTrack(session, timeoutSeconds)
        }
    }

    suspend fun getGroupSnapshot(groupId: String): GroupConfigSnapshot? =
        groupCache?.snapshot(groupId)

    suspend fun close() {
        if (state == State.CLOSED || state == State.CLOSING) return
        state = State.CLOSING
        runCatching { groupCache?.close() }
        runCatching { masqueTransport.close() }
        runCatching { localServer?.close() }
        runCatching { runtime?.close() }
        runCatching { tunnelController.close() }
        runCatching { mediaOffloadAdapter?.close() }
        offloadingSessions.clear()
        state = State.CLOSED
    }

    private suspend fun operation(method: String, path: String, body: JsonObject): OperationResult {
        requireReady()
        val response = runtime!!.request(method, path, body)
        return OperationResult(
            response["success"]?.jsonPrimitive?.booleanOrNull ?: true,
            response["operation_id"]?.jsonPrimitive?.contentOrNull ?: "",
            response["message"]?.jsonPrimitive?.contentOrNull ?: "",
        )
    }

    private fun requireReady() {
        if (state != State.READY) {
            throw AgentSdkException(ErrorCode.SDK_NOT_INITIALIZED, "SDK is not initialized")
        }
    }

    private fun requireOffloadingSession(sessionId: String): OffloadingSession {
        requireReady()
        return offloadingSessions[sessionId]?.takeIf { it.state == "CONNECTED" }
            ?: throw AgentSdkException(
                ErrorCode.OFFLOADING_SESSION_NOT_FOUND,
                "Connected offloading session $sessionId was not found",
            )
    }

    private fun requireMediaAdapter(): MediaOffloadAdapter =
        mediaOffloadAdapter ?: throw AgentSdkException(
            ErrorCode.OFFLOADING_SESSION_NOT_FOUND,
            "No WebRTC media adapter is configured",
        )

    private fun validatePort(port: Int, field: String) {
        if (port !in 1..65535) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "$field must be in 1..65535",
                field,
            )
        }
    }

    private fun JsonObject.requireString(field: String): String =
        this[field]?.jsonPrimitive?.contentOrNull?.takeIf { it.isNotBlank() }
            ?: throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "$field must be a non-empty string",
                field,
            )

    companion object {
        fun create(
            vpnService: AgentVpnService,
            proofVerifier: ProofVerifier,
            messageSigner: MessageSigner = RejectUnconfiguredMessageSigner,
            messageSignatureVerifier: MessageSignatureVerifier =
                RejectUnconfiguredMessageSignatureVerifier,
            mediaOffloadAdapter: MediaOffloadAdapter? = null,
            peerMessenger: PeerMessenger = OkHttpPeerMessenger(),
            localServerFactory: () -> LocalServer = { TcpJsonLocalServer() },
        ): AgentSdk = AgentSdk(
            tunnelController = VpnTunnelController(vpnService),
            masqueTransport = NativeMasqueTransport(NativeMasqueBridge(vpnService)),
            proofVerifier = proofVerifier,
            messageSigner = messageSigner,
            messageSignatureVerifier = messageSignatureVerifier,
            mediaOffloadAdapter = mediaOffloadAdapter,
            peerMessenger = peerMessenger,
            localServerFactory = localServerFactory,
        )
    }
}
