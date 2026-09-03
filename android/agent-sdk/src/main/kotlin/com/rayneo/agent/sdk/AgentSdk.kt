package com.rayneo.agent.sdk

import com.rayneo.agent.sdk.group.GroupMemberCache
import com.rayneo.agent.sdk.masque.NativeMasqueTransport
import com.rayneo.agent.sdk.masque.NativeMasqueBridge
import com.rayneo.agent.sdk.model.AgentProfile
import com.rayneo.agent.sdk.model.AgentLifecycleState
import com.rayneo.agent.sdk.model.DiscoveredAgent
import com.rayneo.agent.sdk.model.GroupConfigSnapshot
import com.rayneo.agent.sdk.model.GroupInfo
import com.rayneo.agent.sdk.model.MessageReceipt
import com.rayneo.agent.sdk.model.NetworkAbility
import com.rayneo.agent.sdk.model.NetworkMessageAction
import com.rayneo.agent.sdk.model.NetworkMessageType
import com.rayneo.agent.sdk.model.OffloadingSession
import com.rayneo.agent.sdk.model.OffloadingSessionRole
import com.rayneo.agent.sdk.model.OperationResult
import com.rayneo.agent.sdk.model.ProcessedVideoEndpoint
import com.rayneo.agent.sdk.model.SdkInitResult
import com.rayneo.agent.sdk.model.VideoUploadEndpoint
import com.rayneo.agent.sdk.security.AndroidDeviceSecurity
import com.rayneo.agent.sdk.security.DisabledMessageSignatureVerifier
import com.rayneo.agent.sdk.security.DisabledProofVerifier
import com.rayneo.agent.sdk.security.RejectUnconfiguredMessageSigner
import com.rayneo.agent.sdk.security.TestCapabilityVcIssuer
import com.rayneo.agent.sdk.security.TEST_CAPABILITY_ISSUER_DID
import com.rayneo.agent.sdk.security.embeddedTestCapabilityIssuerPrivateKeyPem
import com.rayneo.agent.sdk.server.TcpJsonLocalServer
import com.rayneo.agent.sdk.state.AgentStateStore
import com.rayneo.agent.sdk.state.AgentCardContext
import com.rayneo.agent.sdk.state.FileAgentStateStore
import com.rayneo.agent.sdk.state.IdentityApplicationContext
import com.rayneo.agent.sdk.state.InMemoryAgentStateStore
import com.rayneo.agent.sdk.transport.GroupMessageListener
import com.rayneo.agent.sdk.transport.ControlRequestAuthenticator
import com.rayneo.agent.sdk.transport.DevicePublicKeyProvider
import com.rayneo.agent.sdk.transport.LocalServer
import com.rayneo.agent.sdk.transport.LocalAddressResolver
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
import com.rayneo.agent.sdk.transport.RouteLocalAddressResolver
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
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.net.URI
import java.net.InetAddress
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.time.Instant
import java.util.UUID

class AgentSdk internal constructor(
    private val tunnelController: TunnelController,
    private val masqueTransport: MasqueTransport,
    private val proofVerifier: ProofVerifier = DisabledProofVerifier,
    private val controlRequestAuthenticator: ControlRequestAuthenticator? = null,
    private val devicePublicKeyProvider: DevicePublicKeyProvider? = null,
    private val messageSigner: MessageSigner = RejectUnconfiguredMessageSigner,
    private val messageSignatureVerifier: MessageSignatureVerifier = DisabledMessageSignatureVerifier,
    private val peerMessenger: PeerMessenger = OkHttpPeerMessenger(),
    private val runtimeFactory: (String, Int) -> RuntimeTransport = { host, port ->
        OkHttpRuntimeTransport(host, port)
    },
    private val localServerFactory: () -> LocalServer = { TcpJsonLocalServer() },
    private val localAddressResolver: LocalAddressResolver = RouteLocalAddressResolver(),
    private val mediaOffloadAdapter: MediaOffloadAdapter? = null,
    private val testCapabilityVcIssuer: TestCapabilityVcIssuer? = null,
    private val agentStateStore: AgentStateStore = InMemoryAgentStateStore(),
) {
    private enum class State { NEW, INITIALIZING, READY, CLOSING, CLOSED }

    private var state = State.NEW
    private var runtime: RuntimeTransport? = null
    private var computeRuntime: RuntimeTransport? = null
    private var localServer: LocalServer? = null
    private var groupCache: GroupMemberCache? = null
    private var networkListener: NetworkMessageListener? = null
    private var groupListener: GroupMessageListener? = null
    private var profile: AgentProfile? = null
    private var identityApplicationContext: IdentityApplicationContext? = null
    private var agentCardContext: AgentCardContext? = null
    var agentLifecycleState: AgentLifecycleState = AgentLifecycleState.NO_IDENTITY
        private set
    val localProfile: AgentProfile?
        get() = profile
    private var agentRuntimeIp: String = ""
    private var agentRuntimePort: Int = 0
    private var agentTunIp: String = ""
    private var agentTunCidr: String = ""
    private var localTcpPort: Int = 0
    private var localUdpPort: Int = 0
    private val groups = mutableMapOf<String, GroupInfo>()
    private val offloadingSessions = mutableMapOf<String, OffloadingSession>()

    suspend fun initialize(
        agentRuntimeIp: String,
        agentRuntimePort: Int,
        localVlanIp: String? = null,
        localTcpPort: Int,
        localUdpPort: Int,
        masqueServerUrl: String,
        masqueAuthorization: String? = null,
        tunMtu: Int = 1280,
        computeControlIp: String? = null,
        computeControlPort: Int? = null,
    ): SdkInitResult {
        if (state != State.NEW && state != State.CLOSED) {
            throw AgentSdkException(ErrorCode.INVALID_ARGUMENT, "SDK is already initialized")
        }
        validatePort(agentRuntimePort, "agentRuntimePort")
        validatePort(localTcpPort, "localTcpPort")
        validatePort(localUdpPort, "localUdpPort")
        if ((computeControlIp == null) != (computeControlPort == null)) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "computeControlIp and computeControlPort must be configured together",
                "computeControlIp",
            )
        }
        val normalizedComputeControlIp = computeControlIp?.trim()?.let {
            requireIpAddress(it, "computeControlIp", ErrorCode.INVALID_ARGUMENT)
        }
        computeControlPort?.let { validatePort(it, "computeControlPort") }
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
            devicePublicKeyProvider?.ensure()
            runtime = runtimeFactory(agentRuntimeIp, agentRuntimePort)
            this.agentRuntimeIp = agentRuntimeIp
            this.agentRuntimePort = agentRuntimePort
            this.agentTunIp = runtime!!.getUeAgentIp()
            this.agentTunCidr = "$agentTunIp/32"
            agentStateStore.load(agentRuntimeIp, agentRuntimePort, agentTunIp).also {
                agentLifecycleState = it.state
                profile = it.profile
                identityApplicationContext = it.identityApplication
                agentCardContext = it.agentCard
            }
            val masqueOuterSourceIp = localVlanIp
                ?.trim()
                ?.takeIf(String::isNotEmpty)
                ?: withContext(Dispatchers.IO) { localAddressResolver.resolve(uri) }
            tunnelController.establish(
                TunnelConfiguration(this.agentTunCidr, emptySet(), tunMtu)
            )
            groupCache = GroupMemberCache(tunnelController)
            localServer = localServerFactory().also { server ->
                server.start(
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
                    localVlanIp = masqueOuterSourceIp,
                    agentTunCidr = this.agentTunCidr,
                    mtu = tunMtu,
                    identityDirectory = tunnelController.clientIdentityDirectory,
                ),
            )
            tunnelController.setTunFdSwapper(masqueTransport::replaceTunFd)
            computeRuntime = if (normalizedComputeControlIp != null) {
                tunnelController.replaceGroupPeers(
                    COMPUTE_CONTROL_ROUTE_KEY,
                    setOf(normalizedComputeControlIp),
                )
                runtimeFactory(normalizedComputeControlIp, checkNotNull(computeControlPort))
            } else {
                runtime
            }
            state = State.READY
            runtime!!.startDownlink(::handleRuntimeDownlink)
            return SdkInitResult(
                runtimeConnected = true,
                masqueConnected = masqueTransport.connected,
                localTcpEndpoint = "$agentTunIp:$localTcpPort",
                localUdpEndpoint = "$agentTunIp:$localUdpPort",
                agentTcpEndpoint = "$agentTunIp:$localTcpPort",
                agentUdpEndpoint = "$agentTunIp:$localUdpPort",
                agentTunCidr = this.agentTunCidr,
                masqueProxyEndpoint = masqueServerUrl,
                masqueOuterSourceIp = masqueOuterSourceIp,
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
            messageType == "ACN_AGENT_GROUPING_NOTIFICATION" ->
                handleGroupConfig(payload)
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
        val allowedFields = setOf(
            "message_id",
            "group_id",
            "src_agent_id",
            "dst_agent_id",
            "type",
            "task_id",
            "timestamp",
            "payload",
        )
        val unsupportedField = payload.keys.firstOrNull { it !in allowedFields }
        if (unsupportedField != null) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "A2A contains unsupported field: $unsupportedField",
                unsupportedField,
            )
        }
        payload.requireString("message_id")
        val groupId = payload.requireString("group_id")
        val senderId = payload.requireString("src_agent_id")
        payload.requireString("type")
        payload.requireString("task_id")
        payload.requireString("timestamp")
        if (payload.requireString("dst_agent_id") != localProfile.agentId) {
            throw AgentSdkException(
                ErrorCode.TARGET_NOT_IN_GROUP,
                "A2A message targets another agent",
            )
        }
        groupCache!!.resolve(groupId, senderId)
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
        messageType: String,
        taskId: String,
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
        if (messageType.isEmpty()) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "messageType must be a non-empty string",
                "messageType",
            )
        }
        if (taskId.isEmpty()) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "taskId must be a non-empty string",
                "taskId",
            )
        }
        val target = groupCache!!.resolve(groupId, targetAgentId)
        val messageId = UUID.randomUUID().toString()
        val unsigned = buildJsonObject {
            put("message_id", messageId)
            put("group_id", groupId)
            put("type", messageType)
            put("timestamp", Instant.now().toString())
            put("payload", jsonMessage)
            put("src_agent_id", localProfile.agentId)
            put("dst_agent_id", targetAgentId)
            put("task_id", taskId)
        }
        val response = peerMessenger.send(
            target.serviceEndpoint,
            unsigned,
            (timeoutSeconds * 1000).toLong(),
        )
        val delivered = response["status"]?.jsonPrimitive?.contentOrNull == "OK"
        return MessageReceipt(messageId, delivered, if (delivered) Instant.now() else null)
    }

    suspend fun applyIdentity(
        owner: String,
        name: String,
        description: String,
        metadata: JsonObject,
    ): AgentProfile {
        requireReady()
        validateIdentityApplication(owner, name, description, metadata)
        val normalizedMetadata = normalizeIdentityMetadata(metadata)
        val identityApplication = IdentityApplicationContext(
            owner,
            name,
            description,
            normalizedMetadata,
        )
        val publicKey = devicePublicKeyProvider?.publicKeyBase64
            ?: throw AgentSdkException(
                ErrorCode.SIGNATURE_ERROR,
                "SDK device signing identity is unavailable",
            )
        if (agentLifecycleState == AgentLifecycleState.IDENTITY_READY) {
            val previousAgentId = checkNotNull(profile).agentId
            val replacement = deregisterIdentity(previousAgentId, "replaced")
            if (!replacement.success) {
                throw AgentSdkException(
                    ErrorCode.RUNTIME_REJECTED,
                    "Cannot replace local identity because deregistration failed",
                )
            }
        }
        requireAgentState(AgentLifecycleState.NO_IDENTITY, "applyIdentity")
        val path = "/idm/v1/identity-applications"
        val response = runtime!!.request("POST", path, authenticateControl(path, buildJsonObject {
            put("request_id", UUID.randomUUID().toString())
            put("owner", owner)
            put("name", name)
            put("public_key", publicKey)
            put("description", description)
            put("metadata", normalizedMetadata)
        }))
        if (response["result"]?.jsonPrimitive?.contentOrNull != "success") {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime identity response result must be success",
                "result",
            )
        }
        val identityVc = response["vc0"] as? JsonObject ?: buildJsonObject { }
        val responseName = (identityVc["claims"] as? JsonObject)
            ?.get("agent_name")?.jsonPrimitive?.contentOrNull ?: name
        return AgentProfile(
            agentId = response.requireString("agent_id"),
            agentName = responseName,
            identityVc = identityVc,
        ).also {
            persistAgentState(
                AgentLifecycleState.IDENTITY_READY,
                it,
                identityApplication = identityApplication,
            )
        }
    }

    fun restoreLocalProfile(restored: AgentProfile) {
        persistAgentState(
            AgentLifecycleState.IDENTITY_READY,
            restored,
            identityApplication = identityApplicationContext ?: IdentityApplicationContext(
                owner = "restored-local-profile",
                name = restored.agentName,
                description = "Profile restored by the host application",
                metadata = buildJsonObject {
                    put("region", "unknown")
                    put("os", "unknown")
                    put("version", "unknown")
                },
            ),
        )
    }

    suspend fun deregisterIdentity(agentId: String, reason: String = "retired"): OperationResult {
        requireReady()
        requireAgentState(
            setOf(AgentLifecycleState.IDENTITY_READY, AgentLifecycleState.CARD_PUBLISHED),
            "deregisterIdentity",
            agentId,
        )
        if (reason !in setOf(
                "normal", "uninstalled", "replaced", "user_request",
                "security_event", "retired", "other",
            )
        ) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "reason is not a supported deregistration reason",
                "reason",
            )
        }
        return operation("POST", "/acn-agent/v1/agent-deletions", buildJsonObject {
            put("request_id", UUID.randomUUID().toString())
            put("agent_id", agentId)
            put("reason", reason)
        }).also { if (it.success) clearAgentState() }
    }

    /**
     * Returns the local Agent lifecycle to state 1 ([AgentLifecycleState.NO_IDENTITY]).
     *
     * This is a local-only operation: it clears the persisted Profile/Card state
     * without deregistering the identity from the network. State 1 is an
     * idempotent success.
     */
    suspend fun resetAgent(): OperationResult {
        requireReady()
        if (agentLifecycleState == AgentLifecycleState.NO_IDENTITY) {
            return OperationResult(
                success = true,
                operationId = "",
                message = "Agent is already in NO_IDENTITY state",
            )
        }
        clearAgentState()
        return OperationResult(
            success = true,
            operationId = "",
            message = "Local Agent state reset to NO_IDENTITY; network identity was not changed",
        )
    }

    suspend fun getNetworkAbility(
        agentId: String,
        intent: String = "Issue Network Ability Credential",
    ): NetworkAbility {
        requireReady()
        if (intent.isEmpty() || intent.length > 256) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "intent length must be in 1..256",
                "intent",
            )
        }
        val path = "/idm/v1/network-ability"
        val response = runtime!!.request("POST", path, authenticateControl(path, buildJsonObject {
            put("request_id", UUID.randomUUID().toString())
            put("agent_id", agentId)
            put("intent", intent)
        }))
        val abilityVc = response["vc1"] as? JsonObject ?: buildJsonObject { }
        val claims = abilityVc["claims"] as? JsonObject
        val abilities = (claims?.get("network_abilities") as? JsonArray)
            ?.mapNotNull { it.jsonPrimitive.contentOrNull }
            ?: (claims?.get("abilities") as? JsonArray)
            ?.mapNotNull { it.jsonPrimitive.contentOrNull }
            ?: claims?.get("agent_attribute")?.jsonPrimitive?.contentOrNull?.let(::listOf)
            ?: emptyList()
        return NetworkAbility(
            abilityVc,
            abilities,
            abilityVc["valid_until"]?.jsonPrimitive?.contentOrNull?.let(Instant::parse),
        )
    }

    suspend fun registerCapabilities(
        agentId: String,
        priority: Int,
        credentials: List<JsonObject> = emptyList(),
        capabilities: List<String> = emptyList(),
        agentName: String? = null,
    ): OperationResult {
        requireReady()
        requireAgentState(
            setOf(AgentLifecycleState.IDENTITY_READY, AgentLifecycleState.CARD_PUBLISHED),
            "registerCapabilities",
            agentId,
        )
        val vcList = credentials.toMutableList()
        if (capabilities.isNotEmpty()) {
            val resolvedAgentName = agentName
                ?: profile?.takeIf { it.agentId == agentId }?.agentName
                ?: throw AgentSdkException(
                    ErrorCode.INVALID_ARGUMENT,
                    "agentName is required when raw capabilities are published " +
                        "without a matching local profile",
                    "agentName",
                )
            val issuer = testCapabilityVcIssuer ?: throw AgentSdkException(
                ErrorCode.SIGNATURE_ERROR,
                "Test capability VC issuer is unavailable",
                "testCapabilityIssuerPrivateKey",
            )
            vcList += issuer.issue(agentId, resolvedAgentName, capabilities)
        }
        if (vcList.isEmpty()) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "credentials or capabilities must contain at least one item",
                "credentials",
            )
        }
        if (agentLifecycleState == AgentLifecycleState.CARD_PUBLISHED) {
            val identityApplication = identityApplicationContext ?: throw AgentSdkException(
                ErrorCode.AGENT_STATE_INVALID,
                "Cannot replace Agent Card because identity application context is missing",
            )
            ensureCredentialsRebindable(vcList, agentId)
            val deregistered = deregisterIdentity(agentId, "replaced")
            if (!deregistered.success) {
                throw AgentSdkException(
                    ErrorCode.RUNTIME_REJECTED,
                    "Cannot replace Agent Card because identity deregistration failed",
                )
            }
            val newProfile = applyIdentity(
                identityApplication.owner,
                identityApplication.name,
                identityApplication.description,
                identityApplication.metadata,
            )
            val reboundCredentials = refreshReboundCredentials(
                credentials,
                oldAgentId = agentId,
                newProfile = newProfile,
            )
            return registerCapabilities(
                newProfile.agentId,
                priority,
                credentials = reboundCredentials,
                capabilities = capabilities,
                agentName = agentName ?: newProfile.agentName,
            )
        }
        val serviceEndpoints = "http://$agentTunIp:$localTcpPort/A2A/message"
        return operation("POST", "/arf/v1/agent-cards", buildJsonObject {
            put("request_id", UUID.randomUUID().toString())
            put("agent_id", agentId)
            put("priority", priority)
            put("service_endpoints", serviceEndpoints)
            put("vc_list", JsonArray(vcList))
        }).also {
            if (it.success) persistAgentState(
                AgentLifecycleState.CARD_PUBLISHED,
                profile!!,
                agentCard = AgentCardContext(priority, vcList.toList()),
            )
        }
    }

    /** Lab only: import the third-party capability issuer key into app-private storage. */
    fun importTestCapabilityIssuerPrivateKey(privateKeyPem: ByteArray) {
        val issuer = testCapabilityVcIssuer ?: throw AgentSdkException(
            ErrorCode.SIGNATURE_ERROR,
            "Test capability VC issuer is unavailable",
            "testCapabilityIssuerPrivateKey",
        )
        issuer.importPrivateKey(privateKeyPem)
    }

    suspend fun updateCapabilities(
        agentId: String,
        updateItems: List<JsonObject>,
        credentials: List<JsonObject>,
    ): OperationResult {
        requireReady()
        requireAgentState(AgentLifecycleState.CARD_PUBLISHED, "updateCapabilities", agentId)
        val card = agentCardContext ?: throw AgentSdkException(
            ErrorCode.AGENT_STATE_INVALID,
            "Cannot update Agent Card because registration context is missing",
        )
        val replacementVcs = applyCapabilityUpdates(card.vcList, updateItems, credentials)
        return operation("POST", "/arf/v1/agent-cards-update", buildJsonObject {
            put("request_id", UUID.randomUUID().toString())
            put("agent_id", agentId)
            put("update_items", JsonArray(updateItems))
            put("credentials", JsonArray(credentials))
        }).also { result ->
            if (result.success) persistAgentState(
                AgentLifecycleState.CARD_PUBLISHED,
                profile!!,
                agentCard = AgentCardContext(card.priority, replacementVcs),
            )
        }
    }

    suspend fun discoverAgents(
        agentId: String,
        taskDescription: String,
        requiredSkills: List<String>,
        discoveryScope: String = "intra_plmn",
        maxResults: Int = 10,
    ): List<DiscoveredAgent> {
        requireReady()
        val path = "/arf/v1/agent-discoveries"
        val response = runtime!!.request("POST", path, authenticateControl(path, buildJsonObject {
            put("request_id", UUID.randomUUID().toString())
            put("agent_id", agentId)
            put("task_description", taskDescription)
            put("required_skills", buildJsonArray { requiredSkills.forEach { add(JsonPrimitive(it)) } })
            put("discovery_scope", discoveryScope)
            put("max_results", maxResults)
        }))
        return (response["result"] as? JsonArray).orEmpty().map { element ->
            val item = element.jsonObject
            val card = item["agent_card"]?.jsonObject ?: buildJsonObject { }
            DiscoveredAgent(
                agentId = card.requireString("agent_id"),
                serviceEndpoints = card.requireString("service_endpoints"),
                skills = (card["skills"] as? JsonArray).orEmpty().map { it.jsonPrimitive.content },
                priority = item["priority"]?.jsonPrimitive?.intOrNull ?: 0,
            )
        }.sortedBy { it.priority }
    }

    suspend fun createGroup(
        agentId: String,
        targetAgentIds: List<String>,
        groupName: String,
        dnn: String,
        scope: String = "private",
        maxMembers: Int = 10,
    ): GroupInfo {
        requireReady()
        if (dnn.isBlank()) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "dnn must be a non-empty string",
                "dnn",
            )
        }
        val path = "/acf/v1/agents-grouping"
        val response = runtime!!.request("POST", path, authenticateControl(path, buildJsonObject {
            put("request_id", UUID.randomUUID().toString())
            put("agent_id", agentId)
            put("target_agents", buildJsonArray { targetAgentIds.forEach { add(JsonPrimitive(it)) } })
            put("group_config", buildJsonObject {
                put("group_name", groupName)
                put("scope", scope)
                put("max_members", maxMembers)
                put("dnn", dnn)
            })
        }))
        if (response["status"]?.jsonPrimitive?.contentOrNull != "grouped") {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime group response status must be grouped",
                "status",
            )
        }
        return GroupInfo(response.requireString("group_id"), groupName).also {
            groups[it.groupId] = it
        }
    }

    suspend fun createOffloadingSession(
        agentId: String,
        workloadType: String,
        groupId: String,
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
        if (groupId.isBlank()) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "groupId must be a non-empty string",
                "groupId",
            )
        }
        if (profile?.agentId != agentId) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "agentId must match the local Agent identity",
                "agentId",
            )
        }
        groupCache!!.resolve(groupId, agentId)
        val path = "/compute/v1/offloading-sessions"
        val response = withTimeout((timeoutSeconds * 1000).toLong()) {
            requireComputeRuntime().request("POST", path, authenticateControl(path, buildJsonObject {
                put("request_id", UUID.randomUUID().toString())
                put("agent_id", agentId)
                put("workload_type", workloadType)
                put("group_id", groupId)
                sandboxId?.let { put("preferred_sandbox_id", it) }
            }))
        }
        val sessionId = response.requireRuntimeString("session_id")
        val responseGroupId = response["group_id"]?.jsonPrimitive?.contentOrNull ?: groupId
        if (responseGroupId != groupId) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime offloading response group_id does not match the request",
                "group_id",
            )
        }
        val sourceAgentId = response["source_agent_id"]?.jsonPrimitive?.contentOrNull ?: agentId
        if (sourceAgentId != agentId) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime offloading response source_agent_id does not match the creator",
                "source_agent_id",
            )
        }
        val producer = parseVideoUploadEndpoint(
            response["producer"]?.jsonObjectOrNull(),
            ErrorCode.RUNTIME_REJECTED,
            "producer",
        )
        val session = OffloadingSession(
            sessionId,
            response["sandbox_id"]?.jsonPrimitive?.contentOrNull ?: "",
            response["state"]?.jsonPrimitive?.contentOrNull ?: "ALLOCATED",
            response["expires_at"]?.jsonPrimitive?.contentOrNull?.let {
                try {
                    Instant.parse(it)
                } catch (error: Exception) {
                    throw AgentSdkException(
                        ErrorCode.RUNTIME_REJECTED,
                        "Runtime expires_at must be RFC3339",
                        "expires_at",
                        cause = error,
                    )
                }
            },
            buildJsonObject {
                response.filterKeys { it != "producer" }.forEach(::put)
            },
            role = OffloadingSessionRole.PRODUCER,
            groupId = groupId,
            sourceAgentId = agentId,
            producer = producer,
        )
        tunnelController.replaceGroupPeers(
            offloadingRouteKey(sessionId),
            setOf(producer.videoServerIp),
        )
        offloadingSessions[session.sessionId] = session
        return session
    }

    suspend fun startVideoUpload(
        sessionId: String,
        targetAgentIds: List<String>,
        cameraId: String = "0",
        width: Int = 1920,
        height: Int = 1080,
        fps: Int = 30,
        bitrateKbps: Int = 4000,
    ): VideoUploadHandle {
        var session = requireOffloadingSession(sessionId, OffloadingSessionRole.PRODUCER)
        if (session.producer == null) {
            throw AgentSdkException(
                ErrorCode.OFFLOADING_SESSION_INVALID,
                "Producer endpoint is missing for offloading session $sessionId",
            )
        }
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
        val targets = validateOffloadingTargets(session, targetAgentIds)
        var upload: VideoUploadHandle? = null
        try {
            upload = requireMediaAdapter().startVideoUpload(
                session,
                cameraId,
                width,
                height,
                fps,
                bitrateKbps,
            )
            session = session.copy(state = "SOURCE_CONNECTED")
            offloadingSessions[sessionId] = session
            val consumers = requestOffloadingConsumers(session, targets)
            val failed = mutableListOf<String>()
            targets.forEach { targetAgentId ->
                val receipt = sendMessage(
                    session.groupId,
                    targetAgentId,
                    buildOffloadingInvitation(
                        session,
                        targetAgentId,
                        consumers.getValue(targetAgentId),
                    ),
                    messageType = "processed_video_invitation",
                    taskId = "offloading:${session.sessionId}",
                    timeoutSeconds = 10.0,
                )
                if (!receipt.delivered) failed += targetAgentId
            }
            if (failed.isNotEmpty()) {
                throw AgentSdkException(
                    ErrorCode.MESSAGE_DELIVERY_FAILED,
                    "Video Server is pulling, but consumer invitation delivery failed: " +
                        failed.joinToString(),
                )
            }
            return upload
        } catch (error: Throwable) {
            try {
                upload?.stop()
            } catch (_: Throwable) {
                // Preserve the control/P2P failure that triggered rollback.
            }
            offloadingSessions[sessionId] = session.copy(state = "ALLOCATED")
            throw error
        }
    }

    suspend fun acceptOffloadingSession(
        senderAgentId: String,
        groupId: String,
        invitation: JsonObject,
    ): OffloadingSession {
        requireReady()
        val localProfile = profile ?: throw AgentSdkException(
            ErrorCode.GROUP_NOT_ACTIVE,
            "Local identity is unavailable",
        )
        groupCache!!.resolve(groupId, senderAgentId)
        groupCache!!.resolve(groupId, localProfile.agentId)
        if (invitation["type"]?.jsonPrimitive?.contentOrNull != "processed_video_invitation") {
            throw AgentSdkException(
                ErrorCode.OFFLOADING_SESSION_INVALID,
                "Invitation type must be processed_video_invitation",
                "type",
            )
        }
        if (invitation["version"]?.jsonPrimitive?.contentOrNull != "1.0") {
            throw AgentSdkException(
                ErrorCode.OFFLOADING_SESSION_INVALID,
                "Unsupported offloading invitation version",
                "version",
            )
        }
        mapOf(
            "group_id" to groupId,
            "source_agent_id" to senderAgentId,
            "consumer_agent_id" to localProfile.agentId,
        ).forEach { (field, expected) ->
            if (invitation[field]?.jsonPrimitive?.contentOrNull != expected) {
                throw AgentSdkException(
                    ErrorCode.OFFLOADING_SESSION_INVALID,
                    "Invitation $field does not match the authenticated context",
                    field,
                )
            }
        }
        val sessionId = invitation.requireInvitationString("session_id")
        val expiresAt = invitation["expires_at"]?.jsonPrimitive?.contentOrNull?.let {
            try {
                Instant.parse(it)
            } catch (error: Exception) {
                throw AgentSdkException(
                    ErrorCode.OFFLOADING_SESSION_INVALID,
                    "Invitation expires_at must be RFC3339",
                    "expires_at",
                    cause = error,
                )
            }
        }
        if (expiresAt != null && !expiresAt.isAfter(Instant.now())) {
            throw AgentSdkException(
                ErrorCode.CREDENTIAL_EXPIRED,
                "Offloading invitation has expired",
                "expires_at",
            )
        }
        val processedStream = parseProcessedVideoEndpoint(
            invitation["processed_stream"]?.jsonObjectOrNull(),
            ErrorCode.OFFLOADING_SESSION_INVALID,
            "processed_stream",
        )
        val session = OffloadingSession(
            sessionId = sessionId,
            sandboxId = invitation["sandbox_id"]?.jsonPrimitive?.contentOrNull ?: "",
            state = invitation["state"]?.jsonPrimitive?.contentOrNull ?: "SOURCE_CONNECTED",
            expiresAt = expiresAt,
            metadata = buildJsonObject {
                invitation.filterKeys { it != "processed_stream" }.forEach(::put)
            },
            role = OffloadingSessionRole.CONSUMER,
            groupId = groupId,
            sourceAgentId = senderAgentId,
            processedStream = processedStream,
        )
        offloadingSessions[sessionId]?.let { existing ->
            if (existing != session) {
                throw AgentSdkException(
                    ErrorCode.OFFLOADING_SESSION_INVALID,
                    "Offloading session $sessionId is already bound to different metadata",
                )
            }
        }
        tunnelController.replaceGroupPeers(
            offloadingRouteKey(sessionId),
            setOf(processedStream.videoServerIp),
        )
        offloadingSessions[sessionId] = session
        return session
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
        val session = requireOffloadingSession(sessionId, OffloadingSessionRole.CONSUMER)
        if (session.processedStream == null) {
            throw AgentSdkException(
                ErrorCode.OFFLOADING_SESSION_INVALID,
                "Processed stream endpoint is missing for offloading session $sessionId",
            )
        }
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
        if (computeRuntime !== runtime) runCatching { computeRuntime?.close() }
        computeRuntime = null
        runCatching { runtime?.close() }
        runCatching { tunnelController.close() }
        runCatching { mediaOffloadAdapter?.close() }
        offloadingSessions.clear()
        state = State.CLOSED
    }

    private fun applyCapabilityUpdates(
        publishedVcs: List<JsonObject>,
        updateItems: List<JsonObject>,
        credentials: List<JsonObject>,
    ): List<JsonObject> {
        if (updateItems.isEmpty()) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "updateItems must contain at least one item",
                "updateItems",
            )
        }
        var result = publishedVcs.toMutableList()
        fun credentialId(credential: JsonObject): String? =
            credential["id"]?.jsonPrimitive?.contentOrNull?.takeIf(String::isNotEmpty)
        fun addOrReplace(credential: JsonObject) {
            val identifier = credentialId(credential)
            val existing = identifier?.let { id ->
                result.indexOfFirst { credentialId(it) == id }.takeIf { it >= 0 }
            }
            if (existing == null) result += credential else result[existing] = credential
        }
        credentials.forEach(::addOrReplace)
        updateItems.forEachIndexed { index, item ->
            val updateType = item["update_type"]?.jsonPrimitive?.contentOrNull
            val skillName = item["skill_name"]?.jsonPrimitive?.contentOrNull
                ?.takeIf(String::isNotEmpty)
                ?: throw AgentSdkException(
                    ErrorCode.INVALID_ARGUMENT,
                    "skill_name must be a non-empty string",
                    "updateItems[$index].skillName",
                )
            if (updateType !in setOf("add_skill", "remove_skill")) {
                throw AgentSdkException(
                    ErrorCode.INVALID_ARGUMENT,
                    "update_type must be add_skill or remove_skill",
                    "updateItems[$index].updateType",
                )
            }
            val referenceId = item["reference_vc_id"]?.jsonPrimitive?.contentOrNull
            if (updateType == "add_skill") {
                val requiredId = referenceId?.takeIf(String::isNotEmpty)
                    ?: throw AgentSdkException(
                        ErrorCode.INVALID_ARGUMENT,
                        "add_skill requires reference_vc_id",
                        "updateItems[$index].referenceVcId",
                    )
                val referenced = credentials.firstOrNull { credentialId(it) == requiredId }
                    ?: throw AgentSdkException(
                        ErrorCode.INVALID_ARGUMENT,
                        "reference_vc_id was not provided in credentials",
                        "updateItems[$index].referenceVcId",
                    )
                addOrReplace(referenced)
                if (
                    credentialSkillName(referenced) != skillName &&
                    result.none { credentialSkillName(it) == skillName }
                ) {
                    val currentProfile = profile ?: throw AgentSdkException(
                        ErrorCode.AGENT_STATE_INVALID,
                        "Local profile is unavailable while updating the Agent Card snapshot",
                    )
                    val issuer = testCapabilityVcIssuer ?: throw AgentSdkException(
                        ErrorCode.SIGNATURE_ERROR,
                        "Test capability VC issuer is unavailable",
                    )
                    issuer.issue(currentProfile.agentId, currentProfile.agentName, listOf(skillName))
                        .forEach(::addOrReplace)
                }
            } else {
                result = result.filterNot { credential ->
                    credentialSkillName(credential) == skillName ||
                        (referenceId != null && credentialId(credential) == referenceId)
                }.toMutableList()
            }
        }
        if (result.isEmpty()) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "Capability update would produce an empty Agent Card",
                "updateItems",
            )
        }
        return result
    }

    private fun ensureCredentialsRebindable(
        credentials: List<JsonObject>,
        oldAgentId: String,
    ) {
        credentials.forEach { credential ->
            val claims = credential["claims"] as? JsonObject ?: return@forEach
            val network = listOf("network_abilities", "abilities", "agent_attribute")
                .any(claims::containsKey) && !claims.containsKey("skill_name")
            val testCapability = credential["issuer"]?.jsonPrimitive?.contentOrNull ==
                TEST_CAPABILITY_ISSUER_DID &&
                claims["skill_name"]?.jsonPrimitive?.contentOrNull != null
            if (
                claims["agent_id"]?.jsonPrimitive?.contentOrNull == oldAgentId &&
                !network && !testCapability
            ) {
                throw AgentSdkException(
                    ErrorCode.CREDENTIAL_EXPIRED,
                    "A requested capability credential is bound to the current Agent ID " +
                        "but cannot be reissued after identity replacement",
                    "credentials",
                )
            }
        }
    }

    private suspend fun refreshReboundCredentials(
        credentials: List<JsonObject>,
        oldAgentId: String,
        newProfile: AgentProfile,
    ): List<JsonObject> {
        val refreshed = mutableListOf<JsonObject>()
        var networkRefreshed = false
        credentials.forEach { credential ->
            val claims = credential["claims"] as? JsonObject ?: buildJsonObject { }
            val network = listOf("network_abilities", "abilities", "agent_attribute")
                .any(claims::containsKey) && !claims.containsKey("skill_name")
            when {
                network -> if (!networkRefreshed) {
                    refreshed += getNetworkAbility(newProfile.agentId).abilityVc
                    networkRefreshed = true
                }
                credential["issuer"]?.jsonPrimitive?.contentOrNull ==
                    TEST_CAPABILITY_ISSUER_DID &&
                    claims["skill_name"]?.jsonPrimitive?.contentOrNull != null -> {
                    val issuer = testCapabilityVcIssuer ?: throw AgentSdkException(
                        ErrorCode.SIGNATURE_ERROR,
                        "Test capability VC issuer is unavailable",
                    )
                    refreshed += issuer.issue(
                        newProfile.agentId,
                        newProfile.agentName,
                        listOf(claims.getValue("skill_name").jsonPrimitive.content),
                    )
                }
                claims["agent_id"]?.jsonPrimitive?.contentOrNull == oldAgentId ->
                    throw AgentSdkException(
                        ErrorCode.CREDENTIAL_EXPIRED,
                        "An existing capability credential is bound to the deregistered " +
                            "Agent ID and cannot be reissued by the SDK",
                        "credentials",
                    )
                else -> refreshed += credential
            }
        }
        return refreshed
    }

    private fun credentialSkillName(credential: JsonObject): String? =
        (credential["claims"] as? JsonObject)
            ?.get("skill_name")?.jsonPrimitive?.contentOrNull

    private suspend fun operation(method: String, path: String, body: JsonObject): OperationResult {
        requireReady()
        val response = runtime!!.request(method, path, authenticateControl(path, body))
        return OperationResult(
            response["success"]?.jsonPrimitive?.booleanOrNull ?: true,
            response["operation_id"]?.jsonPrimitive?.contentOrNull ?: "",
            response["message"]?.jsonPrimitive?.contentOrNull ?: "",
        )
    }

    private suspend fun authenticateControl(path: String, body: JsonObject): JsonObject {
        val authentication = controlRequestAuthenticator?.authenticate(path, body)
            ?: return body
        val overlap = body.keys.intersect(authentication.keys)
        if (overlap.isNotEmpty()) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "Control authenticator overwrote business fields: ${overlap.sorted()}",
            )
        }
        return buildJsonObject {
            body.forEach(::put)
            authentication.forEach(::put)
        }
    }

    private fun validateIdentityApplication(
        owner: String,
        name: String,
        description: String,
        metadata: JsonObject,
    ) {
        listOf(
            Triple("owner", owner, 128),
            Triple("name", name, 128),
            Triple("description", description, 512),
        ).forEach { (field, value, maximum) ->
            if (value.isEmpty() || value.length > maximum) {
                throw AgentSdkException(
                    ErrorCode.INVALID_ARGUMENT,
                    "$field length must be in 1..$maximum",
                    field,
                )
            }
        }
        if (metadata.any { (_, value) -> value !is JsonPrimitive || !value.isString }) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "metadata keys and values must be strings",
                "metadata",
            )
        }
        listOf("region", "os", "version").forEach { field ->
            val value = metadata[field]?.jsonPrimitive?.contentOrNull
            if (value.isNullOrEmpty()) {
                throw AgentSdkException(
                    ErrorCode.INVALID_ARGUMENT,
                    "metadata.$field must be a non-empty string",
                    "metadata.$field",
                )
            }
        }
    }

    private fun normalizeIdentityMetadata(metadata: JsonObject): JsonObject {
        val required = listOf("region", "os", "version")
        val optional = (metadata.keys - required.toSet()).sorted()
        return buildJsonObject {
            (required + optional).forEach { field ->
                put(field, metadata.getValue(field))
            }
        }
    }

    private fun requireReady() {
        if (state != State.READY) {
            throw AgentSdkException(ErrorCode.SDK_NOT_INITIALIZED, "SDK is not initialized")
        }
    }

    private fun requireAgentState(
        expected: AgentLifecycleState,
        operation: String,
        agentId: String? = null,
    ) = requireAgentState(setOf(expected), operation, agentId)

    private fun requireAgentState(
        expected: Set<AgentLifecycleState>,
        operation: String,
        agentId: String? = null,
    ) {
        if (agentLifecycleState !in expected) {
            throw AgentSdkException(
                ErrorCode.AGENT_STATE_TRANSITION_INVALID,
                "$operation requires ${expected.joinToString { it.name }}; " +
                    "current state is ${agentLifecycleState.name}",
            )
        }
        if (agentId != null && profile?.agentId != agentId) {
            throw AgentSdkException(
                ErrorCode.AGENT_STATE_TRANSITION_INVALID,
                "$operation agentId does not match the persisted local identity",
                "agentId",
            )
        }
    }

    private fun persistAgentState(
        state: AgentLifecycleState,
        storedProfile: AgentProfile,
        identityApplication: IdentityApplicationContext? = null,
        agentCard: AgentCardContext? = null,
    ) {
        if (agentRuntimeIp.isBlank() || agentRuntimePort == 0 || agentTunIp.isBlank()) {
            throw AgentSdkException(
                ErrorCode.SDK_NOT_INITIALIZED,
                "SDK configuration is unavailable for Agent state persistence",
            )
        }
        val resolvedIdentityApplication = identityApplication
            ?: identityApplicationContext
            ?: throw AgentSdkException(
                ErrorCode.AGENT_STATE_INVALID,
                "Identity application context is unavailable",
            )
        agentStateStore.save(
            agentRuntimeIp,
            agentRuntimePort,
            agentTunIp,
            state,
            storedProfile,
            resolvedIdentityApplication,
            agentCard,
        )
        profile = storedProfile
        agentLifecycleState = state
        identityApplicationContext = resolvedIdentityApplication
        agentCardContext = agentCard
    }

    private fun clearAgentState() {
        agentStateStore.clear(agentRuntimeIp, agentRuntimePort)
        profile = null
        agentLifecycleState = AgentLifecycleState.NO_IDENTITY
        identityApplicationContext = null
        agentCardContext = null
    }

    private fun requireOffloadingSession(
        sessionId: String,
        role: OffloadingSessionRole,
    ): OffloadingSession {
        requireReady()
        val session = offloadingSessions[sessionId]
            ?.takeUnless { it.state in setOf("CLOSED", "FAILED", "STOPPED") }
            ?: throw AgentSdkException(
                ErrorCode.OFFLOADING_SESSION_NOT_FOUND,
                "Active offloading session $sessionId was not found",
            )
        if (session.role != role) {
            throw AgentSdkException(
                ErrorCode.OFFLOADING_ROLE_INVALID,
                "Offloading session $sessionId has role ${session.role}; $role is required",
            )
        }
        return session
    }

    private suspend fun validateOffloadingTargets(
        session: OffloadingSession,
        targetAgentIds: List<String>,
    ): List<String> {
        if (targetAgentIds.isEmpty() || targetAgentIds.any { it.isBlank() }) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "targetAgentIds must contain non-empty Agent IDs",
                "targetAgentIds",
            )
        }
        if (targetAgentIds.distinct().size != targetAgentIds.size) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "targetAgentIds must not contain duplicates",
                "targetAgentIds",
            )
        }
        if (session.sourceAgentId in targetAgentIds) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "The source Agent cannot be a video consumer",
                "targetAgentIds",
            )
        }
        targetAgentIds.forEach { groupCache!!.resolve(session.groupId, it) }
        return targetAgentIds.toList()
    }

    private suspend fun requestOffloadingConsumers(
        session: OffloadingSession,
        targetAgentIds: List<String>,
    ): Map<String, ProcessedVideoEndpoint> {
        val encodedSessionId = URLEncoder.encode(
            session.sessionId,
            StandardCharsets.UTF_8.toString(),
        ).replace("+", "%20")
        val path = "/compute/v1/offloading-sessions/$encodedSessionId/consumers"
        val response = requireComputeRuntime().request("POST", path, authenticateControl(path, buildJsonObject {
            put("request_id", UUID.randomUUID().toString())
            put("agent_id", session.sourceAgentId)
            put("group_id", session.groupId)
            put("target_agent_ids", buildJsonArray {
                targetAgentIds.forEach { add(JsonPrimitive(it)) }
            })
        }))
        val consumers = response["consumers"]?.jsonObjectOrNull()
            ?: throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime response field consumers must be an object",
                "consumers",
            )
        val endpoints = targetAgentIds.associateWith { targetAgentId ->
            parseProcessedVideoEndpoint(
                consumers[targetAgentId]?.jsonObjectOrNull(),
                ErrorCode.RUNTIME_REJECTED,
                "consumers.$targetAgentId",
            )
        }
        if (endpoints.values.map { it.accessTicket }.distinct().size != endpoints.size) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime returned a consumer ticket shared by multiple Agents",
                "consumers",
            )
        }
        return endpoints
    }

    private fun buildOffloadingInvitation(
        session: OffloadingSession,
        consumerAgentId: String,
        endpoint: ProcessedVideoEndpoint,
    ): JsonObject = buildJsonObject {
        put("type", "processed_video_invitation")
        put("version", "1.0")
        put("session_id", session.sessionId)
        put("group_id", session.groupId)
        put("source_agent_id", session.sourceAgentId)
        put("consumer_agent_id", consumerAgentId)
        put("sandbox_id", session.sandboxId)
        put("state", "SOURCE_CONNECTED")
        session.expiresAt?.let { put("expires_at", it.toString()) }
        put("processed_stream", buildJsonObject {
            put("video_server_ip", endpoint.videoServerIp)
            put("offer_url", endpoint.offerUrl)
            put("access_ticket", endpoint.accessTicket)
            put("protocol", endpoint.protocol)
            put("signaling", endpoint.signaling)
        })
    }

    private fun parseVideoUploadEndpoint(
        value: JsonObject?,
        errorCode: ErrorCode,
        fieldPrefix: String,
    ): VideoUploadEndpoint {
        val endpoint = value ?: throw AgentSdkException(
            errorCode,
            "$fieldPrefix must be an object",
            fieldPrefix,
        )
        return VideoUploadEndpoint(
            videoServerIp = requireIpAddress(
                endpoint.stringOrNull("video_server_ip"),
                "$fieldPrefix.video_server_ip",
                errorCode,
            ),
            sourceStartUrl = requireHttpUrl(
                endpoint.stringOrNull("source_start_url"),
                "$fieldPrefix.source_start_url",
                errorCode,
            ),
            sourceStopUrl = requireHttpUrl(
                endpoint.stringOrNull("source_stop_url"),
                "$fieldPrefix.source_stop_url",
                errorCode,
            ),
            accessToken = requireEndpointString(
                endpoint.stringOrNull("access_token"),
                "$fieldPrefix.access_token",
                errorCode,
            ),
        )
    }

    private fun parseProcessedVideoEndpoint(
        value: JsonObject?,
        errorCode: ErrorCode,
        fieldPrefix: String,
    ): ProcessedVideoEndpoint {
        val endpoint = value ?: throw AgentSdkException(
            errorCode,
            "$fieldPrefix must be an object",
            fieldPrefix,
        )
        val protocol = endpoint.stringOrNull("protocol") ?: "webrtc"
        val signaling = endpoint.stringOrNull("signaling") ?: "non-trickle"
        if (protocol != "webrtc" || signaling !in setOf("non-trickle", "trickle")) {
            throw AgentSdkException(
                errorCode,
                "$fieldPrefix contains an unsupported WebRTC profile",
                fieldPrefix,
            )
        }
        return ProcessedVideoEndpoint(
            videoServerIp = requireIpAddress(
                endpoint.stringOrNull("video_server_ip"),
                "$fieldPrefix.video_server_ip",
                errorCode,
            ),
            offerUrl = requireHttpUrl(
                endpoint.stringOrNull("offer_url"),
                "$fieldPrefix.offer_url",
                errorCode,
            ),
            accessTicket = requireEndpointString(
                endpoint.stringOrNull("access_ticket"),
                "$fieldPrefix.access_ticket",
                errorCode,
            ),
            protocol = protocol,
            signaling = signaling,
        )
    }

    private fun requireIpAddress(value: String?, field: String, errorCode: ErrorCode): String {
        val text = requireEndpointString(value, field, errorCode)
        val parsed = try {
            InetAddress.getByName(text)
        } catch (error: Exception) {
            throw AgentSdkException(errorCode, "$field must be an IP address", field, cause = error)
        }
        if (text.any { it.isLetter() } && !text.contains(':')) {
            throw AgentSdkException(errorCode, "$field must be an IP address", field)
        }
        return parsed.hostAddress ?: text
    }

    private fun requireHttpUrl(value: String?, field: String, errorCode: ErrorCode): String {
        val text = requireEndpointString(value, field, errorCode)
        val uri = try {
            URI(text)
        } catch (error: Exception) {
            throw AgentSdkException(errorCode, "$field is not a valid URL", field, cause = error)
        }
        if (
            uri.scheme !in setOf("http", "https") || uri.host.isNullOrBlank() ||
            uri.userInfo != null || uri.fragment != null ||
            (uri.port != -1 && uri.port !in 1..65535)
        ) {
            throw AgentSdkException(
                errorCode,
                "$field must be an HTTP/HTTPS URL without credentials or fragment",
                field,
            )
        }
        return text
    }

    private fun requireEndpointString(
        value: String?,
        field: String,
        errorCode: ErrorCode,
    ): String = value?.takeIf { it.isNotBlank() } ?: throw AgentSdkException(
        errorCode,
        "$field must be a non-empty string",
        field,
    )

    private fun JsonObject.stringOrNull(field: String): String? =
        this[field]?.jsonPrimitive?.contentOrNull

    private fun kotlinx.serialization.json.JsonElement.jsonObjectOrNull(): JsonObject? =
        runCatching { jsonObject }.getOrNull()

    private fun JsonObject.requireRuntimeString(field: String): String =
        stringOrNull(field)?.takeIf { it.isNotBlank() } ?: throw AgentSdkException(
            ErrorCode.RUNTIME_REJECTED,
            "Runtime response field $field must be a non-empty string",
            field,
        )

    private fun JsonObject.requireInvitationString(field: String): String =
        stringOrNull(field)?.takeIf { it.isNotBlank() } ?: throw AgentSdkException(
            ErrorCode.OFFLOADING_SESSION_INVALID,
            "Invitation field $field must be a non-empty string",
            field,
        )

    private fun offloadingRouteKey(sessionId: String): String = "offloading:$sessionId"

    private fun requireMediaAdapter(): MediaOffloadAdapter =
        mediaOffloadAdapter ?: throw AgentSdkException(
            ErrorCode.OFFLOADING_SESSION_NOT_FOUND,
            "No WebRTC media adapter is configured",
        )

    private fun requireComputeRuntime(): RuntimeTransport =
        computeRuntime ?: throw AgentSdkException(
            ErrorCode.SDK_NOT_INITIALIZED,
            "Compute control transport is unavailable",
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
        private const val COMPUTE_CONTROL_ROUTE_KEY = "compute-control"

        fun create(
            vpnService: AgentVpnService,
            mediaOffloadAdapter: MediaOffloadAdapter? = null,
            peerMessenger: PeerMessenger = OkHttpPeerMessenger(),
            localServerFactory: () -> LocalServer = { TcpJsonLocalServer() },
        ): AgentSdk {
            val security = vpnService.resources.openRawResource(
                R.raw.core_network_public_key
            ).use(AndroidDeviceSecurity::create)
            val testCapabilityIssuer = TestCapabilityVcIssuer(
                File(
                    vpnService.noBackupFilesDir,
                    "agent-sdk/test-capability-vc/issuer-private-key.pem",
                )
            ).also {
                it.importPrivateKey(embeddedTestCapabilityIssuerPrivateKeyPem())
            }
            return AgentSdk(
                tunnelController = VpnTunnelController(vpnService),
                masqueTransport = NativeMasqueTransport(NativeMasqueBridge(vpnService)),
                proofVerifier = DisabledProofVerifier,
                controlRequestAuthenticator = security,
                devicePublicKeyProvider = security,
                messageSigner = security,
                messageSignatureVerifier = DisabledMessageSignatureVerifier,
                testCapabilityVcIssuer = testCapabilityIssuer,
                agentStateStore = FileAgentStateStore(
                    File(vpnService.noBackupFilesDir, "agent-sdk/agents")
                ),
                mediaOffloadAdapter = mediaOffloadAdapter,
                peerMessenger = peerMessenger,
                localServerFactory = localServerFactory,
            )
        }
    }
}
