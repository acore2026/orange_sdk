package com.rayneo.agent.sdk

import android.util.Log
import com.rayneo.agent.sdk.group.GroupMemberCache
import com.rayneo.agent.sdk.masque.NativeMasqueTransport
import com.rayneo.agent.sdk.masque.NativeMasqueBridge
import com.rayneo.agent.sdk.model.AgentProfile
import com.rayneo.agent.sdk.model.AudioControlActionRequest
import com.rayneo.agent.sdk.model.AudioTranscriptionRequest
import com.rayneo.agent.sdk.model.AudioTranscriptionResult
import com.rayneo.agent.sdk.model.AudioTranscriptionSegment
import com.rayneo.agent.sdk.model.IntentRecognitionResult
import com.rayneo.agent.sdk.model.AcnContext
import com.rayneo.agent.sdk.model.AgentLifecycleState
import com.rayneo.agent.sdk.model.ComputeConnectionParameters
import com.rayneo.agent.sdk.model.ComputeConstraints
import com.rayneo.agent.sdk.model.ComputeInputFormat
import com.rayneo.agent.sdk.model.ComputeNetworkBinding
import com.rayneo.agent.sdk.model.ComputeRequestType
import com.rayneo.agent.sdk.model.ComputeResources
import com.rayneo.agent.sdk.model.ComputeRole
import com.rayneo.agent.sdk.model.ComputeSessionRequest
import com.rayneo.agent.sdk.model.ComputeSessionStatus
import com.rayneo.agent.sdk.model.ComputingContext
import com.rayneo.agent.sdk.model.ComputingSession
import com.rayneo.agent.sdk.model.ControlAction
import com.rayneo.agent.sdk.model.ControlActionRequest
import com.rayneo.agent.sdk.model.ControlActionStatus
import com.rayneo.agent.sdk.model.ControlInputType
import com.rayneo.agent.sdk.model.ControlTargetRole
import com.rayneo.agent.sdk.model.DiscoveredAgent
import com.rayneo.agent.sdk.model.GroupConfigSnapshot
import com.rayneo.agent.sdk.model.GroupInfo
import com.rayneo.agent.sdk.model.MessageReceipt
import com.rayneo.agent.sdk.model.NetworkAbility
import com.rayneo.agent.sdk.model.NetworkMessageAction
import com.rayneo.agent.sdk.model.NetworkMessageType
import com.rayneo.agent.sdk.model.OperationResult
import com.rayneo.agent.sdk.model.RecognitionTarget
import com.rayneo.agent.sdk.model.RecognitionTargetStatus
import com.rayneo.agent.sdk.model.RuntimeDataPlane
import com.rayneo.agent.sdk.model.SdkInitResult
import com.rayneo.agent.sdk.model.Snssai
import com.rayneo.agent.sdk.model.VoiceTranscription
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
import com.rayneo.agent.sdk.transport.LocalProcessedVideo
import com.rayneo.agent.sdk.transport.MasqueConfiguration
import com.rayneo.agent.sdk.transport.MasqueTransport
import com.rayneo.agent.sdk.transport.MasqueTransportStatistics
import com.rayneo.agent.sdk.transport.MessageSignatureVerifier
import com.rayneo.agent.sdk.transport.MessageSigner
import com.rayneo.agent.sdk.transport.NetworkMessageListener
import com.rayneo.agent.sdk.transport.OkHttpPeerMessenger
import com.rayneo.agent.sdk.transport.OkHttpRuntimeTransport
import com.rayneo.agent.sdk.transport.OkHttpSandboxTransport
import com.rayneo.agent.sdk.transport.PeerMessenger
import com.rayneo.agent.sdk.transport.ProofVerifier
import com.rayneo.agent.sdk.transport.RuntimeTransport
import com.rayneo.agent.sdk.transport.SandboxTransport
import com.rayneo.agent.sdk.transport.RouteLocalAddressResolver
import com.rayneo.agent.sdk.transport.RuntimeHttpResponse
import com.rayneo.agent.sdk.transport.TunnelConfiguration
import com.rayneo.agent.sdk.transport.TunnelController
import com.rayneo.agent.sdk.transport.VideoTrack
import com.rayneo.agent.sdk.transport.VideoUploadHandle
import com.rayneo.agent.sdk.transport.ProcessedVideoStream
import com.rayneo.agent.sdk.vpn.AgentVpnService
import com.rayneo.agent.sdk.vpn.VpnTunnelController
import com.rayneo.agent.sdk.webrtc.AndroidWebRtcMediaAdapter
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.encodeToJsonElement
import kotlinx.serialization.json.Json
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.selects.select
import java.io.File
import java.net.URI
import java.net.InetAddress
import java.time.Instant
import java.util.UUID

private class ManagedVideoUpload(
    private val inner: VideoUploadHandle,
    private val closeRemote: suspend () -> Unit,
) : VideoUploadHandle {
    private var localStopped = false
    private var remoteClosed = false
    override val trackId: String get() = inner.trackId
    override val state: String get() = inner.state
    override suspend fun pause() = inner.pause()
    override suspend fun resume() = inner.resume()
    override suspend fun stop() {
        if (localStopped && remoteClosed) return
        var localError: Throwable? = null
        if (!localStopped) {
            try {
                inner.stop()
                localStopped = true
            } catch (error: Throwable) { localError = error }
        }
        if (!remoteClosed) {
            closeRemote()
            remoteClosed = true
        }
        localError?.let { throw it }
    }
}

private class ManagedProcessedVideoStream(
    private val inner: LocalProcessedVideo,
    private val closeRemote: suspend () -> Unit,
) : ProcessedVideoStream {
    private var localClosed = false
    private var remoteClosed = false
    override val track: VideoTrack get() = inner.track
    override val state: String get() = if (localClosed) "CLOSED" else "RUNNING"
    override suspend fun close() {
        if (localClosed && remoteClosed) return
        var localError: Throwable? = null
        if (!localClosed) {
            try {
                inner.close()
                localClosed = true
            } catch (error: Throwable) { localError = error }
        }
        if (!remoteClosed) {
            closeRemote()
            remoteClosed = true
        }
        localError?.let { throw it }
    }
}

private data class MediaConnectionRecord(
    val session: ComputingSession,
    val requestId: String,
    val mediaConnectionId: String,
    val managed: Any,
)

private data class CreatedMediaConnection(
    val requestId: String,
    val mediaConnectionId: String,
    val answerSdp: String,
)

private data class PendingMediaConnection(
    val session: ComputingSession,
    val requestId: String,
    val offerSdp: String,
    val prepared: com.rayneo.agent.sdk.transport.PreparedMediaConnection<*>,
)

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
    private val sandboxTransport: SandboxTransport = OkHttpSandboxTransport(),
    private val testCapabilityVcIssuer: TestCapabilityVcIssuer? = null,
    private val agentStateStore: AgentStateStore = InMemoryAgentStateStore(),
) {
    private enum class State { NEW, INITIALIZING, READY, CLOSING, CLOSED }

    private var state = State.NEW
    private var runtime: RuntimeTransport? = null
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
    private var ueInfo: JsonObject? = null
    private val computeRequests = mutableMapOf<String, JsonObject>()
    private val computeCreateRequests = mutableMapOf<String, ComputeSessionRequest>()
    private val computingStatuses = mutableMapOf<String, ComputeSessionStatus>()
    private val computingStatusesByRequest = mutableMapOf<String, ComputeSessionStatus>()
    private val computingSessions = mutableMapOf<String, ComputingSession>()
    private val computingMedia = mutableMapOf<String, MediaConnectionRecord>()
    private val computingPendingMedia = mutableMapOf<String, PendingMediaConnection>()
    private val computingMediaLocks = mutableMapOf<String, Mutex>()
    private val computingClosing = mutableSetOf<String>()
    private val computingCloseResults = mutableMapOf<Triple<String, String, String>, JsonObject>()
    private val computingWaiters = mutableMapOf<String, MutableList<CompletableDeferred<ComputingSession>>>()
    private val computingStatusWaiters =
        mutableMapOf<String, MutableList<CompletableDeferred<ComputeSessionStatus>>>()
    private val computingCloseWaiters =
        mutableMapOf<String, MutableList<CompletableDeferred<Unit>>>()
    private val computingRequestsInFlight = mutableSetOf<String>()
    private val computingClosedSessionIds = mutableSetOf<String>()
    private val computingControlMutex = Mutex()
    private val computingMutex = Mutex()
    private val identityRemovalMutex = Mutex()
    private var identityRemovalInProgress = false
    private val receivedA2aMutex = Mutex()
    private val receivedA2aMessageIds = linkedSetOf<String>()
    private val computeJson = Json

    /** Uses [agentRuntimeIp] and [agentRuntimePort] for every control-plane request. */
    suspend fun initialize(
        agentRuntimeIp: String,
        agentRuntimePort: Int,
        localVlanIp: String? = null,
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
            devicePublicKeyProvider?.ensure()
            runtime = runtimeFactory(agentRuntimeIp, agentRuntimePort)
            this.agentRuntimeIp = agentRuntimeIp
            this.agentRuntimePort = agentRuntimePort
            ueInfo = runtime!!.getUeInfo()
            this.agentTunIp = selectDefaultUeAgentIp(checkNotNull(ueInfo))
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
            tunnelController.setTunReplacedListener(::rebindLocalServerAfterTunReplacement)
            state = State.READY
            runtime!!.startDownlink(
                onReconnected = ::recoverComputeStatuses,
                handler = ::handleRuntimeDownlink,
            )
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
        val changed = cache.commit(candidate, localProfile.agentId)
        if (!changed) {
            Log.i(
                TAG,
                "Acknowledging identical group config replay without side effects " +
                    "group_id=${candidate.groupId} timestamp=${candidate.notificationTimestamp}",
            )
            return NetworkMessageAction.ACK
        }
        synchronized(groups) {
            groups.getOrPut(candidate.groupId) {
                GroupInfo(candidate.groupId, candidate.groupId)
            }.status = "ACTIVE"
        }
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
    ): JsonObject? {
        if (transactionId !in 1..255) {
            return buildJsonObject { put("result", NetworkMessageAction.REJECT.name) }
        }
        return when {
            messageType == COMPUTE_CONNECT_CONFIG -> computingControlMutex.withLock {
                handleComputeConnectConfig(payload)
            }
            messageType == COMPUTE_SESSION_STATUS -> {
                computingControlMutex.withLock { handleComputeSessionStatus(payload) }
                null
            }
            messageType == COMPUTE_SESSION_CLOSE -> computingControlMutex.withLock {
                handleComputeSessionClose(payload)
            }
            messageType == "ACN_AGENT_GROUPING_INVITATION" -> {
                val action = handleGroupInvitation(payload)
                buildJsonObject {
                    val groupInfo = payload["group_info"] as? JsonObject
                    groupInfo?.stringOrNull("group_id")?.let { put("group_id", it) }
                    put("result", action.name)
                }
            }
            messageType == "ACN_AGENT_GROUPING_NOTIFICATION" -> {
                val action = handleGroupConfig(payload)
                buildJsonObject {
                    payload.stringOrNull("group_id")?.let { put("group_id", it) }
                    put("result", action.name)
                }
            }
            else -> buildJsonObject {
                put(
                    "result",
                    (
                        networkListener?.onNetworkMessage(NetworkMessageType.UNKNOWN, payload)
                            ?: NetworkMessageAction.REJECT
                    ).name,
                )
            }
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
        val messageId = payload.requireString("message_id")
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
        receivedA2aMutex.withLock {
            if (messageId in receivedA2aMessageIds) {
                Log.i(TAG, "Acknowledging duplicate A2A message_id=$messageId without redispatch")
                return@withLock
            }
            receivedA2aMessageIds += messageId
            try {
                listener.onGroupMessage(groupId, senderId, userPayload)
            } catch (error: Throwable) {
                receivedA2aMessageIds -= messageId
                throw error
            }
            while (receivedA2aMessageIds.size > RECEIVED_A2A_MESSAGE_CACHE_LIMIT) {
                val oldest = receivedA2aMessageIds.iterator()
                oldest.next()
                oldest.remove()
            }
        }
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
        return identityRemovalMutex.withLock {
            beginIdentityRemoval("deregisterIdentity")
            try {
                operation("POST", "/acn-agent/v1/agent-deletions", buildJsonObject {
                    put("request_id", UUID.randomUUID().toString())
                    put("agent_id", agentId)
                    put("reason", reason)
                }).also { if (it.success) clearAgentState() }
            } finally {
                computingMutex.withLock { identityRemovalInProgress = false }
            }
        }
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
        return identityRemovalMutex.withLock {
            beginIdentityRemoval("resetAgent")
            try {
                clearAgentState()
                OperationResult(
                    success = true,
                    operationId = "",
                    message = "Local Agent state reset to NO_IDENTITY; network identity was not changed",
                )
            } finally {
                computingMutex.withLock { identityRemovalInProgress = false }
            }
        }
    }

    private suspend fun beginIdentityRemoval(operation: String) {
        computingControlMutex.withLock {
            computingMutex.withLock {
                val activeSessionIds = activeComputingSessionIds()
                val activeRequestIds = (
                    computingRequestsInFlight + unresolvedComputeCreateRequestIds()
                    ).distinct().sorted()
                if (activeSessionIds.isNotEmpty() || activeRequestIds.isNotEmpty()) {
                    val details = buildList {
                        addAll(activeSessionIds)
                        addAll(activeRequestIds.map { "request:$it" })
                    }
                    throw AgentSdkException(
                        ErrorCode.AGENT_STATE_INVALID,
                        "$operation requires all computing sessions to be released or cancelled " +
                            "before clearing the Agent Profile: ${details.joinToString()}",
                    )
                }
                identityRemovalInProgress = true
            }
        }
    }

    private fun activeComputingSessionIds(): List<String> =
        (
            computingSessions.keys + computingPendingMedia.keys + computingMedia.keys +
                computingStatuses.filter { (sessionId, status) ->
                    sessionId !in computingClosedSessionIds &&
                        status.status !in COMPUTE_TERMINAL_STATUSES
                }.keys
            )
            .distinct()
            .sorted()

    private fun unresolvedComputeCreateRequestIds(): List<String> =
        computeCreateRequests.keys.filter { requestId ->
            val status = computingStatusesByRequest[requestId]
            val sessionId = status?.computeServiceSessionId
            val sessionStatus = sessionId?.let(computingStatuses::get)
            status == null || !(
                status.status in COMPUTE_TERMINAL_STATUSES ||
                    sessionStatus?.status in COMPUTE_TERMINAL_STATUSES ||
                    sessionId?.let { it in computingClosedSessionIds } == true
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
        val groupId = response.requireString("group_id")
        val hasCommittedConfig = groupCache?.snapshot(groupId) != null
        return synchronized(groups) {
            val status = if (
                hasCommittedConfig || groups[groupId]?.status == "ACTIVE"
            ) "ACTIVE" else "PENDING"
            GroupInfo(groupId, groupName, status).also {
                groups[groupId] = it
            }
        }
    }

    suspend fun createComputingSession(
        request: ComputeSessionRequest,
        timeoutSeconds: Double = 30.0,
    ): ComputeSessionStatus {
        validateComputeRequest(request, ComputeRequestType.CREATE)
        val context = checkNotNull(request.acnContext)
        if (profile == null || context.requesterAgentId != profile!!.agentId) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "acn_context.requester_agent_id must match the local Agent",
                "acn_context.requester_agent_id",
            )
        }
        val snapshot = groupCache?.snapshot(context.groupId)
        val groupActive = synchronized(groups) {
            groups[context.groupId]?.status == "ACTIVE"
        }
        if (snapshot == null || !groupActive) {
            throw AgentSdkException(
                ErrorCode.GROUP_NOT_ACTIVE,
                "Group ${context.groupId} is not ACTIVE",
                "acn_context.group_id",
            )
        }
        if (!snapshot.membersByAgentId.containsKey(context.targetAgentId)) {
            throw AgentSdkException(
                ErrorCode.TARGET_NOT_IN_GROUP,
                "acn_context.target_agent_id is not in the configured group",
                "acn_context.target_agent_id",
            )
        }
        return sendComputeRequest(request, timeoutSeconds)
    }

    suspend fun queryComputingSession(
        request: ComputeSessionRequest,
        timeoutSeconds: Double = 30.0,
    ): ComputeSessionStatus {
        validateComputeRequest(request, ComputeRequestType.QUERY)
        return sendComputeRequest(request, timeoutSeconds)
    }

    suspend fun cancelComputingSession(
        request: ComputeSessionRequest,
        timeoutSeconds: Double = 30.0,
    ): ComputeSessionStatus {
        validateComputeRequest(request, ComputeRequestType.CANCEL)
        return sendComputeRequest(request, timeoutSeconds)
    }

    suspend fun releaseComputingSession(
        request: ComputeSessionRequest,
        timeoutSeconds: Double = 30.0,
    ): ComputeSessionStatus {
        validateComputeRequest(request, ComputeRequestType.RELEASE)
        return sendComputeRequest(request, timeoutSeconds)
    }

    suspend fun awaitComputingSessionClosed(
        computeServiceSessionId: String,
        timeoutSeconds: Double = 30.0,
    ) {
        requireReady()
        requireComputeString(computeServiceSessionId, "compute_service_session_id")
        if (timeoutSeconds <= 0.0) {
            invalidCompute("timeoutSeconds must be greater than zero", "timeoutSeconds")
        }
        val waiter = computingMutex.withLock {
            if (computeServiceSessionId !in activeComputingSessionIds()) return
            CompletableDeferred<Unit>().also {
                computingCloseWaiters.getOrPut(computeServiceSessionId) { mutableListOf() } += it
            }
        }
        try {
            withTimeout((timeoutSeconds * 1000).toLong()) { waiter.await() }
        } catch (error: TimeoutCancellationException) {
            throw AgentSdkException(
                ErrorCode.TIMEOUT,
                "Timed out waiting for C-05 to close computing session $computeServiceSessionId",
                retryable = true,
                cause = error,
            )
        } finally {
            computingMutex.withLock {
                computingCloseWaiters[computeServiceSessionId]?.let { waiters ->
                    waiters.remove(waiter)
                    if (waiters.isEmpty()) computingCloseWaiters.remove(computeServiceSessionId)
                }
            }
        }
    }

    suspend fun startVideoUpload(
        computeServiceSessionId: String,
        cameraId: String = "0",
        width: Int = 1920,
        height: Int = 1080,
        fps: Int = 30,
        bitrateKbps: Int = 4000,
        timeoutSeconds: Double = 120.0,
    ): VideoUploadHandle {
        if (timeoutSeconds <= 0.0) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "timeoutSeconds must be greater than zero",
                "timeoutSeconds",
            )
        }
        requireReady()
        val session = waitForComputingSession(computeServiceSessionId, timeoutSeconds)
        if (session.role != ComputeRole.producer) {
            throw AgentSdkException(
                ErrorCode.COMPUTING_SESSION_INVALID,
                "startVideoUpload requires the producer configuration",
                "role",
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
        val adapter = requireMediaAdapter()
        validateMediaCodec(adapter, session)
        val lock = computingMutex.withLock {
            computingMediaLocks.getOrPut(computeServiceSessionId) { Mutex() }
        }
        return lock.withLock {
            computingMutex.withLock { computingMedia[computeServiceSessionId] }
                ?.let { return@withLock it.managed as VideoUploadHandle }
            var pending = computingMutex.withLock {
                computingPendingMedia[computeServiceSessionId]
            }
            if (pending == null) {
                val newlyPrepared = adapter.prepareVideoUpload(
                    session, cameraId, width, height, fps, bitrateKbps, timeoutSeconds,
                )
                try {
                    validateLocalMediaOffer(session, newlyPrepared.offerSdp)
                } catch (error: Throwable) {
                    newlyPrepared.abort()
                    throw error
                }
                pending = PendingMediaConnection(
                    session,
                    "media-${session.role.name}-${UUID.randomUUID()}",
                    newlyPrepared.offerSdp,
                    newlyPrepared,
                )
                computingMutex.withLock {
                    computingPendingMedia[computeServiceSessionId] = pending
                }
            }
            @Suppress("UNCHECKED_CAST")
            val prepared = pending.prepared as
                com.rayneo.agent.sdk.transport.PreparedMediaConnection<VideoUploadHandle>
            var created: CreatedMediaConnection? = null
            try {
                val connection = createMediaConnection(
                    session, pending.requestId, pending.offerSdp, timeoutSeconds,
                )
                created = connection
                if (computingMutex.withLock { computeServiceSessionId in computingClosing }) {
                    throw AgentSdkException(
                        ErrorCode.COMPUTING_SESSION_INVALID,
                        "computing session closed during media negotiation",
                    )
                }
                installMediaCandidateRoutes(session, connection.answerSdp)
                val local = prepared.applyAnswer(connection.answerSdp, timeoutSeconds)
                val managed = ManagedVideoUpload(local) {
                    closeMediaRecord(
                        computeServiceSessionId, connection.mediaConnectionId, timeoutSeconds,
                    )
                }
                val stored = computingMutex.withLock {
                    computingPendingMedia.remove(computeServiceSessionId)
                    if (computeServiceSessionId in computingClosing) false else {
                        computingMedia[computeServiceSessionId] = MediaConnectionRecord(
                            session, connection.requestId, connection.mediaConnectionId, managed,
                        )
                        true
                    }
                }
                if (!stored) {
                    local.stop()
                    throw AgentSdkException(
                        ErrorCode.COMPUTING_SESSION_INVALID,
                        "computing session closed during media negotiation",
                    )
                }
                managed
            } catch (error: Throwable) {
                val preservePending = created == null &&
                    error is AgentSdkException && error.retryable &&
                    computingMutex.withLock { computeServiceSessionId !in computingClosing }
                if (preservePending) throw error
                computingMutex.withLock { computingPendingMedia.remove(computeServiceSessionId) }
                withContext(NonCancellable) {
                    runCatching { prepared.abort() }
                    created?.let {
                        runCatching {
                            deleteMediaConnection(session, it.mediaConnectionId, timeoutSeconds)
                        }
                        if (computingMutex.withLock {
                                computeServiceSessionId !in computingClosing
                            }
                        ) runCatching { replaceComputeRoutes(session) }
                    }
                }
                throw error
            }
        }
    }

    suspend fun getProcessedVideoStream(
        computeServiceSessionId: String,
        timeoutSeconds: Double = 120.0,
    ): ProcessedVideoStream {
        if (timeoutSeconds <= 0.0) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "timeoutSeconds must be greater than zero",
                "timeoutSeconds",
            )
        }
        requireReady()
        val session = waitForComputingSession(computeServiceSessionId, timeoutSeconds)
        if (session.role != ComputeRole.consumer) {
            throw AgentSdkException(
                ErrorCode.COMPUTING_SESSION_INVALID,
                "getProcessedVideoStream requires the consumer configuration",
                "role",
            )
        }
        val adapter = requireMediaAdapter()
        validateMediaCodec(adapter, session)
        val lock = computingMutex.withLock {
            computingMediaLocks.getOrPut(computeServiceSessionId) { Mutex() }
        }
        return lock.withLock {
                computingMutex.withLock { computingMedia[computeServiceSessionId] }
                    ?.let { return@withLock it.managed as ProcessedVideoStream }
                var pending = computingMutex.withLock {
                    computingPendingMedia[computeServiceSessionId]
                }
                if (pending == null) {
                    val newlyPrepared = adapter.prepareProcessedVideo(session, timeoutSeconds)
                    try {
                        validateLocalMediaOffer(session, newlyPrepared.offerSdp)
                    } catch (error: Throwable) {
                        newlyPrepared.abort()
                        throw error
                    }
                    pending = PendingMediaConnection(
                        session,
                        "media-${session.role.name}-${UUID.randomUUID()}",
                        newlyPrepared.offerSdp,
                        newlyPrepared,
                    )
                    computingMutex.withLock {
                        computingPendingMedia[computeServiceSessionId] = pending
                    }
                }
                @Suppress("UNCHECKED_CAST")
                val prepared = pending.prepared as
                    com.rayneo.agent.sdk.transport.PreparedMediaConnection<LocalProcessedVideo>
                var created: CreatedMediaConnection? = null
                try {
                    val connection = createMediaConnection(
                        session, pending.requestId, pending.offerSdp, timeoutSeconds,
                    )
                    created = connection
                    if (computingMutex.withLock { computeServiceSessionId in computingClosing }) {
                        throw AgentSdkException(
                            ErrorCode.COMPUTING_SESSION_INVALID,
                            "computing session closed during media negotiation",
                        )
                    }
                    installMediaCandidateRoutes(session, connection.answerSdp)
                    val local = prepared.applyAnswer(connection.answerSdp, timeoutSeconds)
                    val managed = ManagedProcessedVideoStream(local) {
                        closeMediaRecord(
                            computeServiceSessionId, connection.mediaConnectionId, timeoutSeconds,
                        )
                    }
                    val stored = computingMutex.withLock {
                        computingPendingMedia.remove(computeServiceSessionId)
                        if (computeServiceSessionId in computingClosing) false else {
                            computingMedia[computeServiceSessionId] = MediaConnectionRecord(
                                session, connection.requestId, connection.mediaConnectionId, managed,
                            )
                            true
                        }
                    }
                    if (!stored) {
                        local.close()
                        throw AgentSdkException(
                            ErrorCode.COMPUTING_SESSION_INVALID,
                            "computing session closed during media negotiation",
                        )
                    }
                    managed
                } catch (error: Throwable) {
                    val preservePending = created == null &&
                        error is AgentSdkException && error.retryable &&
                        computingMutex.withLock { computeServiceSessionId !in computingClosing }
                    if (preservePending) throw error
                    computingMutex.withLock { computingPendingMedia.remove(computeServiceSessionId) }
                    withContext(NonCancellable) {
                        runCatching { prepared.abort() }
                        created?.let {
                            runCatching {
                                deleteMediaConnection(session, it.mediaConnectionId, timeoutSeconds)
                            }
                            if (computingMutex.withLock {
                                    computeServiceSessionId !in computingClosing
                                }
                            ) runCatching { replaceComputeRoutes(session) }
                        }
                    }
                    throw error
                }
        }
    }

    suspend fun updateRecognitionTarget(
        computeServiceSessionId: String,
        requestId: String,
        text: String,
        language: String? = null,
        timeoutSeconds: Double = 15.0,
    ): RecognitionTargetStatus {
        validateSandboxTimeout(timeoutSeconds)
        validateComputeRequestId(requestId, "request_id")
        requireComputeString(text, "text")
        language?.let { requireComputeString(it, "language") }
        val session = waitForComputingSession(computeServiceSessionId, timeoutSeconds)
        requireConsumerSession(session, "updateRecognitionTarget")
        val context = mediaContext(session)
        val body = buildJsonObject {
            put("request_id", requestId)
            put("computing_context", context)
            put("input", buildJsonObject {
                put("type", "TEXT")
                put("text", text)
                language?.let { put("language", it) }
            })
        }
        val response = sandboxTransport.requestWithStatus(
            "PUT",
            recognitionTargetUrl(session),
            body,
            timeoutSeconds,
            session.networkBinding.ueIpv4,
        )
        return parseRecognitionTargetResponse(response, session, requestId)
    }

    suspend fun getRecognitionTarget(
        computeServiceSessionId: String,
        timeoutSeconds: Double = 15.0,
    ): RecognitionTargetStatus {
        validateSandboxTimeout(timeoutSeconds)
        val session = waitForComputingSession(computeServiceSessionId, timeoutSeconds)
        requireConsumerSession(session, "getRecognitionTarget")
        val response = sandboxTransport.requestWithStatus(
            "GET",
            recognitionTargetUrl(session),
            null,
            timeoutSeconds,
            session.networkBinding.ueIpv4,
        )
        return parseRecognitionTargetResponse(response, session)
    }

    suspend fun createControlAction(
        computeServiceSessionId: String,
        request: ControlActionRequest,
        timeoutSeconds: Double = 15.0,
    ): ControlActionStatus {
        validateSandboxTimeout(timeoutSeconds)
        validateControlActionRequest(request)
        val session = waitForComputingSession(computeServiceSessionId, timeoutSeconds)
        requireConsumerSession(session, "createControlAction")
        val body = buildJsonObject {
            put("request_id", request.requestId)
            put("computing_context", mediaContext(session))
            request.action?.let { put("action", it.name) }
            put("input", buildJsonObject {
                put("type", request.inputType.name)
                if (request.inputType == ControlInputType.TEXT) {
                    put("text", checkNotNull(request.text))
                    request.language?.let { put("language", it) }
                }
            })
            request.parameters?.let { put("parameters", it) }
            request.target?.let { target ->
                put("target", buildJsonObject {
                    put("role", target.role.name)
                    target.agentId?.let { put("agent_id", it) }
                })
            }
        }
        val response = sandboxTransport.requestWithStatus(
            "POST",
            controlActionsUrl(session),
            body,
            timeoutSeconds,
            session.networkBinding.ueIpv4,
        )
        return parseControlActionResponse(
            response = response,
            session = session,
            expectedHttpStatus = 202,
            expectedRequestId = request.requestId,
            requireContext = true,
            requireNormalized = request.inputType == ControlInputType.TEXT,
        )
    }

    suspend fun createAudioControlAction(
        computeServiceSessionId: String,
        request: AudioControlActionRequest,
        timeoutSeconds: Double = 120.0,
    ): ControlActionStatus {
        validateSandboxTimeout(timeoutSeconds)
        validateComputeRequestId(request.requestId, "request_id")
        val fileName = validateAudioUpload(request.audio, request.fileName, request.contentType)
        requireComputeString(request.language, "language")
        request.stopReason?.let { requireComputeString(it, "stopReason") }
        val session = waitForComputingSession(computeServiceSessionId, timeoutSeconds)
        requireConsumerSession(session, "createAudioControlAction")
        val fields = buildMap {
            put("request_id", request.requestId)
            put("computing_context", mediaContext(session).toString())
            put("language", request.language)
            request.stopReason?.let { put("stop_reason", it) }
        }
        val response = sandboxTransport.uploadWithStatus(
            url = audioControlActionsUrl(session),
            fields = fields,
            fileFieldName = "file",
            fileName = fileName,
            contentType = request.contentType,
            content = request.audio,
            timeoutSeconds = timeoutSeconds,
            sourceIpv4 = session.networkBinding.ueIpv4,
        )
        return parseControlActionResponse(
            response = response,
            session = session,
            expectedHttpStatus = 202,
            expectedRequestId = request.requestId,
            requireContext = true,
            requireNormalized = true,
            requireTranscription = true,
        )
    }

    /**
     * Uploads one audio clip to the standalone ASR helper exposed by pruned_sandbox on port 9004.
     * This operation deliberately does not require [initialize] or a C-02 computing binding.
     */
    suspend fun transcribeAudio(
        asrUrl: String,
        request: AudioTranscriptionRequest,
        timeoutSeconds: Double = 120.0,
    ): AudioTranscriptionResult {
        validateSandboxTimeout(timeoutSeconds)
        val endpoint = runCatching { URI(asrUrl) }.getOrNull()
        if (endpoint == null || endpoint.scheme !in setOf("http", "https") ||
            endpoint.host.isNullOrBlank()
        ) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "asrUrl must be an absolute HTTP or HTTPS URL",
                "asrUrl",
            )
        }
        val fileName = validateAudioUpload(
            audio = request.audio,
            fileName = request.fileName,
            contentType = request.contentType,
        )
        val sessionId = requireComputeString(request.sessionId, "sessionId")
        val taskId = requireComputeString(request.taskId, "taskId")
        val source = requireComputeString(request.source, "source")
        request.language?.let { requireComputeString(it, "language") }
        request.stopReason?.let { requireComputeString(it, "stopReason") }
        val fields = buildMap {
            put("session_id", sessionId)
            put("task_id", taskId)
            put("source", source)
            request.language?.let { put("language", it) }
            request.stopReason?.let { put("stop_reason", it) }
        }
        val response = sandboxTransport.uploadWithStatus(
            url = endpoint.toASCIIString(),
            fields = fields,
            fileFieldName = "file",
            fileName = fileName,
            contentType = request.contentType,
            content = request.audio,
            timeoutSeconds = timeoutSeconds,
            sourceIpv4 = null,
        )
        return parseAudioTranscriptionResponse(response)
    }

    /**
     * Classifies one text command with the standalone pruned_sandbox intent service.
     * This operation does not require [initialize] or a C-02 computing binding.
     *
     * The service currently returns internal classifier names such as `patrol`; this method
     * normalizes them to the public business contract such as `security patrol` and exposes
     * extracted arguments through [IntentRecognitionResult.slots].
     */
    suspend fun recognizeIntent(
        intentUrl: String,
        text: String,
        timeoutSeconds: Double = 15.0,
    ): IntentRecognitionResult {
        validateSandboxTimeout(timeoutSeconds)
        val endpoint = requireStandaloneHttpUrl(intentUrl, "intentUrl")
        val normalizedText = requireComputeString(text, "text")
        val response = sandboxTransport.requestWithStatus(
            method = "POST",
            url = endpoint.toASCIIString(),
            body = buildJsonObject { put("text", normalizedText) },
            timeoutSeconds = timeoutSeconds,
            sourceIpv4 = null,
        )
        return parseIntentRecognitionResponse(response)
    }

    suspend fun getControlAction(
        computeServiceSessionId: String,
        actionId: String,
        timeoutSeconds: Double = 15.0,
    ): ControlActionStatus {
        validateSandboxTimeout(timeoutSeconds)
        val normalizedActionId = requireComputeString(actionId, "action_id")
        val session = waitForComputingSession(computeServiceSessionId, timeoutSeconds)
        requireConsumerSession(session, "getControlAction")
        val encodedActionId = URI(null, null, "/$normalizedActionId", null)
            .rawPath.removePrefix("/")
        val response = sandboxTransport.requestWithStatus(
            "GET",
            "${controlActionsUrl(session).trimEnd('/')}/$encodedActionId",
            null,
            timeoutSeconds,
            session.networkBinding.ueIpv4,
        )
        return parseControlActionResponse(
            response = response,
            session = session,
            expectedHttpStatus = 200,
            expectedActionId = normalizedActionId,
        )
    }

    private fun validateAudioUpload(
        audio: ByteArray,
        fileName: String,
        contentType: String,
    ): String {
        if (audio.isEmpty()) {
            throw AgentSdkException(ErrorCode.INVALID_ARGUMENT, "audio must not be empty", "audio")
        }
        if (audio.size > MAX_AUDIO_UPLOAD_BYTES) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "audio must not exceed 50 MiB",
                "audio",
            )
        }
        val normalizedFileName = fileName.replace('\\', '/').substringAfterLast('/')
        if (normalizedFileName.isBlank()) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "fileName must be a non-empty file name",
                "fileName",
            )
        }
        val suffix = normalizedFileName.substringAfterLast('.', missingDelimiterValue = "")
            .lowercase()
        if (suffix !in SUPPORTED_AUDIO_SUFFIXES) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "audio file must use wav, mp3, m4a, flac, ogg, or webm",
                "fileName",
            )
        }
        requireComputeString(contentType, "contentType")
        return normalizedFileName
    }

    private fun parseAudioTranscriptionResponse(
        response: RuntimeHttpResponse,
    ): AudioTranscriptionResult {
        if (response.statusCode != 200) {
            val detail = response.body.stringOrNull("message")
                ?: (response.body["error"] as? JsonObject)?.stringOrNull("message")
            throw AgentSdkException(
                ErrorCode.SANDBOX_REJECTED,
                detail?.takeIf(String::isNotBlank)
                    ?: "ASR returned HTTP ${response.statusCode}",
                retryable = response.statusCode >= 500,
            )
        }
        fun requiredString(field: String, allowEmpty: Boolean = false): String {
            val value = response.body.stringOrNull(field)
                ?: invalidControlResponse("$field must be a string", field)
            if (!allowEmpty && value.isBlank()) {
                invalidControlResponse("$field must be a non-empty string", field)
            }
            return value
        }
        fun requiredLong(field: String): Long {
            val value = response.body[field]?.jsonPrimitive?.longOrNull
                ?: invalidControlResponse("$field must be an integer", field)
            if (value < 0) invalidControlResponse("$field must not be negative", field)
            return value
        }
        fun nullableString(field: String): String? {
            val raw = response.body[field] ?: return null
            if (raw is JsonNull) return null
            return response.body.stringOrNull(field)
                ?: invalidControlResponse("$field must be null or a string", field)
        }
        val segments = (response.body["segments"] as? JsonArray)
            ?.mapIndexed { index, raw ->
                val value = raw as? JsonObject ?: invalidControlResponse(
                    "segments[$index] must be an object",
                    "segments",
                )
                val start = value["startSec"]?.jsonPrimitive?.doubleOrNull
                    ?: invalidControlResponse(
                        "segments[$index].startSec must be a number",
                        "segments",
                    )
                val end = value["endSec"]?.jsonPrimitive?.doubleOrNull
                    ?: invalidControlResponse(
                        "segments[$index].endSec must be a number",
                        "segments",
                    )
                val text = value.stringOrNull("text")
                    ?: invalidControlResponse(
                        "segments[$index].text must be a string",
                        "segments",
                    )
                AudioTranscriptionSegment(start, end, text)
            }
            ?: invalidControlResponse("segments must be an array", "segments")
        val probabilityRaw = response.body["languageProbability"]
        val probability = when (probabilityRaw) {
            null, JsonNull -> null
            else -> probabilityRaw.jsonPrimitive.doubleOrNull
                ?: invalidControlResponse(
                    "languageProbability must be null or a number",
                    "languageProbability",
                )
        }
        return AudioTranscriptionResult(
            transcriptId = requiredString("transcriptId"),
            sessionId = requiredString("sessionId"),
            taskId = requiredString("taskId"),
            source = requiredString("source"),
            text = requiredString("text", allowEmpty = true),
            language = nullableString("language"),
            languageProbability = probability,
            durationMs = requiredLong("durationMs"),
            processingMs = requiredLong("processingMs"),
            createdAtMs = requiredLong("createdAtMs"),
            stopReason = nullableString("stopReason"),
            segments = segments,
            audioFilename = requiredString("audioFilename"),
        )
    }

    private fun parseIntentRecognitionResponse(
        response: RuntimeHttpResponse,
    ): IntentRecognitionResult {
        if (response.statusCode != 200) {
            val detail = response.body.stringOrNull("message")
                ?: (response.body["error"] as? JsonObject)?.stringOrNull("message")
            throw AgentSdkException(
                ErrorCode.SANDBOX_REJECTED,
                detail?.takeIf(String::isNotBlank)
                    ?: "Intent service returned HTTP ${response.statusCode}",
                retryable = response.statusCode >= 500,
            )
        }

        val nested = response.body["intent"] as? JsonObject
        val payload = nested ?: response.body
        val rawIntent = payload.stringOrNull("intent")
            ?.trim()
            ?.lowercase()
            ?.takeIf(String::isNotBlank)
            ?: invalidControlResponse("intent must be a non-empty string", "intent")
        val scene = when (rawIntent) {
            "security patrol" -> "patrol"
            "find object" -> "find_object"
            else -> response.body.stringOrNull("scene")?.trim()?.lowercase()
                ?.takeIf(String::isNotBlank) ?: rawIntent
        }
        val publicIntent = when (scene) {
            "patrol" -> "security patrol"
            "find_object" -> "find object"
            else -> rawIntent
        }
        val argument = response.body.stringOrNull("normalized_argument")
            ?: response.body.stringOrNull("argument")
        val slots = buildMap {
            val area = payload.stringOrNull("area")
                ?: argument?.takeIf { scene == "patrol" }?.removeSuffix("区域")?.trim()
            val direction = payload.stringOrNull("direction")
                ?: argument?.takeIf { scene == "movement" }
            val objectName = payload.stringOrNull("object")
                ?: argument?.takeIf { scene in setOf("find_object", "grab") }
            area?.takeIf(String::isNotBlank)?.let { put("area", it) }
            direction?.takeIf(String::isNotBlank)?.let { put("direction", it) }
            objectName?.takeIf(String::isNotBlank)?.let { put("object", it) }
        }
        val matched = payload["matched"]?.jsonPrimitive?.booleanOrNull
            ?: (scene != "other")
        val confidence = response.body["confidence"]?.jsonPrimitive?.doubleOrNull
        val status = response.body.stringOrNull("status") ?: "success"
        return IntentRecognitionResult(
            status = status,
            intent = publicIntent,
            scene = scene,
            executor = payload.stringOrNull("executor"),
            slots = slots,
            matched = matched,
            confidence = confidence,
            backend = payload.stringOrNull("backend") ?: response.body.stringOrNull("backend"),
        )
    }

    private fun requireStandaloneHttpUrl(value: String, field: String): URI {
        val endpoint = runCatching { URI(value) }.getOrNull()
        if (endpoint == null || endpoint.scheme !in setOf("http", "https") ||
            endpoint.host.isNullOrBlank()
        ) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "$field must be an absolute HTTP or HTTPS URL",
                field,
            )
        }
        return endpoint
    }

    private fun validateControlActionRequest(request: ControlActionRequest) {
        validateComputeRequestId(request.requestId, "request_id")
        if (request.inputType == ControlInputType.TEXT) {
            requireComputeString(request.text, "text")
            request.language?.let { requireComputeString(it, "language") }
            if (request.parameters != null) {
                throw AgentSdkException(
                    ErrorCode.INVALID_ARGUMENT,
                    "TEXT control input must not contain parameters",
                    "parameters",
                )
            }
        } else {
            if (request.text != null || request.language != null) {
                throw AgentSdkException(
                    ErrorCode.INVALID_ARGUMENT,
                    "STRUCTURED control input must not contain text or language",
                    "text",
                )
            }
            if (request.action == null) {
                throw AgentSdkException(
                    ErrorCode.INVALID_ARGUMENT,
                    "STRUCTURED control input requires action",
                    "action",
                )
            }
            if (request.parameters == null) {
                throw AgentSdkException(
                    ErrorCode.INVALID_ARGUMENT,
                    "STRUCTURED control input requires a parameters object",
                    "parameters",
                )
            }
        }
        request.target?.let { target ->
            if (target.role == ControlTargetRole.producer) {
                requireComputeString(target.agentId, "target.agent_id")
            } else {
                target.agentId?.let { requireComputeString(it, "target.agent_id") }
            }
        }
    }

    private fun parseControlActionResponse(
        response: com.rayneo.agent.sdk.transport.RuntimeHttpResponse,
        session: ComputingSession,
        expectedHttpStatus: Int,
        expectedRequestId: String? = null,
        expectedActionId: String? = null,
        requireContext: Boolean = false,
        requireNormalized: Boolean = false,
        requireTranscription: Boolean = false,
    ): ControlActionStatus {
        if (response.statusCode != expectedHttpStatus) {
            val error = response.body["error"] as? JsonObject
            throw AgentSdkException(
                ErrorCode.SANDBOX_REJECTED,
                error?.stringOrNull("message")
                    ?: "Sandbox returned HTTP ${response.statusCode}",
                retryable = response.statusCode >= 500,
            )
        }
        val requestId = response.body.stringOrNull("request_id")
            ?.takeIf(String::isNotBlank)
            ?: invalidControlResponse("request_id must be a non-empty string", "request_id")
        if (expectedRequestId != null && requestId != expectedRequestId) {
            invalidControlResponse(
                "response request_id does not match the control request",
                "request_id",
            )
        }
        val actionId = response.body.stringOrNull("action_id")
            ?.takeIf(String::isNotBlank)
            ?: invalidControlResponse("action_id must be a non-empty string", "action_id")
        if (expectedActionId != null && actionId != expectedActionId) {
            invalidControlResponse("response action_id does not match the query", "action_id")
        }
        val rawContext = response.body["computing_context"]
        if (requireContext && rawContext == null) {
            invalidControlResponse("response computing_context is required", "computing_context")
        }
        if (rawContext != null && rawContext != mediaContext(session)) {
            invalidControlResponse(
                "response computing_context does not match C-02",
                "computing_context",
            )
        }
        val status = response.body.stringOrNull("status")
            ?.takeIf { it in CONTROL_ACTION_STATUSES }
            ?: invalidControlResponse("status is not a defined control action status", "status")
        val cause = response.body.stringOrNull("cause")
            ?: invalidControlResponse("cause must be a string", "cause")
        val normalizedAction = response.body.stringOrNull("normalized_action")?.let { value ->
            runCatching { ControlAction.valueOf(value) }.getOrElse {
                invalidControlResponse(
                    "normalized_action is not a defined action",
                    "normalized_action",
                )
            }
        }
        val normalizedParameters = response.body["normalized_parameters"]?.let {
            it as? JsonObject
                ?: invalidControlResponse(
                    "normalized_parameters must be an object",
                    "normalized_parameters",
                )
        }
        if (requireNormalized && (normalizedAction == null || normalizedParameters == null)) {
            invalidControlResponse(
                "TEXT control response requires normalized_action and normalized_parameters",
                "normalized_action",
            )
        }
        val result = response.body["result"]?.let {
            it as? JsonObject ?: invalidControlResponse("result must be an object", "result")
        }
        val transcription = response.body["transcription"]?.let { raw ->
            val value = raw as? JsonObject
                ?: invalidControlResponse("transcription must be an object", "transcription")
            val text = value.stringOrNull("text")?.takeIf(String::isNotBlank)
                ?: invalidControlResponse(
                    "transcription.text must be a non-empty string",
                    "transcription.text",
                )
            val language = value.stringOrNull("language")?.takeIf(String::isNotBlank)
                ?: invalidControlResponse(
                    "transcription.language must be a non-empty string",
                    "transcription.language",
                )
            val transcriptId = value["transcript_id"]?.let { rawId ->
                if (rawId is JsonNull) null else {
                    value.stringOrNull("transcript_id")?.takeIf(String::isNotBlank)
                        ?: invalidControlResponse(
                            "transcription.transcript_id must be null or a non-empty string",
                            "transcription.transcript_id",
                        )
                }
            }
            VoiceTranscription(text, language, transcriptId)
        }
        if (requireTranscription && transcription == null) {
            invalidControlResponse(
                "audio control response requires transcription",
                "transcription",
            )
        }
        val context = if (rawContext == null) null else ComputingContext(
            computeServiceSessionId = session.computeServiceSessionId,
            computeInstanceId = session.computeInstanceId,
            bindingRef = session.bindingRef,
            role = session.role,
            agentId = session.receiverAgentId,
        )
        return ControlActionStatus(
            requestId = requestId,
            actionId = actionId,
            status = status,
            cause = cause,
            computingContext = context,
            normalizedAction = normalizedAction,
            normalizedParameters = normalizedParameters,
            result = result,
            transcription = transcription,
        )
    }

    private fun invalidControlResponse(message: String, field: String): Nothing =
        throw AgentSdkException(ErrorCode.SANDBOX_REJECTED, message, field)

    private fun validateSandboxTimeout(timeoutSeconds: Double) {
        if (timeoutSeconds <= 0.0) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "timeoutSeconds must be greater than zero",
                "timeoutSeconds",
            )
        }
    }

    private fun requireConsumerSession(session: ComputingSession, operation: String) {
        if (session.role != ComputeRole.consumer) {
            throw AgentSdkException(
                ErrorCode.COMPUTING_SESSION_INVALID,
                "$operation requires the consumer configuration",
                "role",
            )
        }
    }

    private fun parseRecognitionTargetResponse(
        response: com.rayneo.agent.sdk.transport.RuntimeHttpResponse,
        session: ComputingSession,
        expectedRequestId: String? = null,
    ): RecognitionTargetStatus {
        if (response.statusCode != 200) {
            val error = response.body["error"] as? JsonObject
            throw AgentSdkException(
                ErrorCode.SANDBOX_REJECTED,
                error?.stringOrNull("message")
                    ?: "Sandbox returned HTTP ${response.statusCode}",
                retryable = response.statusCode >= 500,
            )
        }
        val requestId = response.body.stringOrNull("request_id")
            ?.takeIf(String::isNotBlank)
            ?: invalidRecognitionResponse("request_id must be a non-empty string", "request_id")
        if (expectedRequestId != null && requestId != expectedRequestId) {
            invalidRecognitionResponse(
                "response request_id does not match the recognition request",
                "request_id",
            )
        }
        if (response.body["computing_context"] != mediaContext(session)) {
            invalidRecognitionResponse(
                "response computing_context does not match C-02",
                "computing_context",
            )
        }
        if (response.body.stringOrNull("status") != "APPLIED") {
            invalidRecognitionResponse("recognition target status must be APPLIED", "status")
        }
        val revision = response.body.stringOrNull("target_revision")
            ?.takeIf { it.toULongOrNull() != null }
            ?: invalidRecognitionResponse(
                "target_revision must be a uint64 decimal string",
                "target_revision",
            )
        val target = response.body["target"] as? JsonObject
            ?: invalidRecognitionResponse("target must be an object", "target")
        val label = target.stringOrNull("label")?.takeIf(String::isNotBlank)
            ?: invalidRecognitionResponse("target.label must be a non-empty string", "target.label")
        val prompt = target.stringOrNull("prompt")?.takeIf(String::isNotBlank)
            ?: invalidRecognitionResponse(
                "target.prompt must be a non-empty string",
                "target.prompt",
            )
        return RecognitionTargetStatus(
            requestId = requestId,
            computingContext = ComputingContext(
                computeServiceSessionId = session.computeServiceSessionId,
                computeInstanceId = session.computeInstanceId,
                bindingRef = session.bindingRef,
                role = session.role,
                agentId = session.receiverAgentId,
            ),
            status = "APPLIED",
            targetRevision = revision,
            target = RecognitionTarget(label, prompt),
        )
    }

    private fun invalidRecognitionResponse(message: String, field: String): Nothing =
        throw AgentSdkException(ErrorCode.SANDBOX_REJECTED, message, field)

    private fun validateMediaCodec(adapter: MediaOffloadAdapter, session: ComputingSession) {
        val codec = session.connectionParameters.videoCodec ?: return
        if (!adapter.supportsVideoCodec(codec)) {
            throw AgentSdkException(
                ErrorCode.MEDIA_NEGOTIATION_FAILED,
                "required video codec is not supported: $codec",
                "connection_parameters.video_codec",
            )
        }
    }

    private fun mediaContext(session: ComputingSession): JsonObject = buildJsonObject {
        put("compute_service_session_id", session.computeServiceSessionId)
        put("compute_instance_id", session.computeInstanceId)
        put("binding_ref", session.bindingRef)
        put("role", session.role.name)
        put("agent_id", session.receiverAgentId)
    }

    private suspend fun createMediaConnection(
        session: ComputingSession,
        requestId: String,
        offerSdp: String,
        timeoutSeconds: Double,
    ): CreatedMediaConnection {
        val context = mediaContext(session)
        val body = buildJsonObject {
            put("request_id", requestId)
            put("computing_context", context)
            put("offer", buildJsonObject {
                put("type", "offer")
                put("sdp", offerSdp)
            })
        }
        var response: com.rayneo.agent.sdk.transport.RuntimeHttpResponse? = null
        for (attempt in 0..1) {
            try {
                response = sandboxTransport.requestWithStatus(
                    "POST", mediaConnectionsUrl(session), body, timeoutSeconds,
                    session.networkBinding.ueIpv4,
                )
                break
            } catch (error: AgentSdkException) {
                if (attempt == 1 || !error.retryable) throw error
            }
        }
        val actual = checkNotNull(response)
        if (actual.statusCode != 201) {
            val error = actual.body["error"] as? JsonObject
            throw AgentSdkException(
                ErrorCode.MEDIA_NEGOTIATION_FAILED,
                error?.stringOrNull("message")
                    ?: "Sandbox returned HTTP ${actual.statusCode}",
                retryable = actual.statusCode >= 500,
            )
        }
        val rawConnectionId = actual.body.stringOrNull("media_connection_id")
        val connectionId: String
        val answerSdp: String
        try {
            if (actual.body.stringOrNull("request_id") != requestId) {
                invalidMediaResponse("response request_id does not match the request")
            }
            if (actual.body["computing_context"] != context) {
                invalidMediaResponse("response computing_context does not match C-02")
            }
            connectionId = rawConnectionId
                ?: invalidMediaResponse("media_connection_id must be a non-empty string")
            val answer = actual.body["answer"] as? JsonObject
                ?: invalidMediaResponse("answer must be an object")
            if (answer.stringOrNull("type") != "answer") {
                invalidMediaResponse("answer.type must be answer")
            }
            answerSdp = answer.stringOrNull("sdp")
                ?: invalidMediaResponse("answer.sdp must be a non-empty string")
        } catch (error: AgentSdkException) {
            rawConnectionId?.let {
                runCatching { deleteMediaConnection(session, it, timeoutSeconds) }
            }
            throw error
        }
        return CreatedMediaConnection(requestId, connectionId, answerSdp)
    }

    private fun invalidMediaResponse(message: String): Nothing =
        throw AgentSdkException(ErrorCode.MEDIA_NEGOTIATION_FAILED, message)

    private fun validateLocalMediaOffer(session: ComputingSession, offerSdp: String) {
        val expected = if (session.role == ComputeRole.producer) "a=sendonly" else "a=recvonly"
        if (offerSdp.lineSequence().none { it.trimEnd('\r') == expected }) {
            invalidMediaResponse("local WebRTC Offer must contain $expected")
        }
        val all = candidateIpv4s(offerSdp)
        if (session.networkBinding.ueIpv4 !in all) {
            invalidMediaResponse(
                "local WebRTC Offer has no ICE candidate for the C-02 UE IPv4 address",
            )
        }
        if (candidateIpv4s(offerSdp, hostOnly = true) != setOf(session.networkBinding.ueIpv4)) {
            invalidMediaResponse(
                "local WebRTC Offer contains a host candidate outside the C-02 user plane",
            )
        }
    }

    private fun candidateIpv4s(sdp: String, hostOnly: Boolean = false): Set<String> =
        sdp.lineSequence().mapNotNull { raw ->
            val line = raw.trimEnd('\r')
            if (!line.startsWith("a=candidate:")) return@mapNotNull null
            val parts = line.split(Regex("\\s+"))
            if (parts.size < 6 || (hostOnly && (parts.size < 8 || parts[7].lowercase() != "host"))) {
                return@mapNotNull null
            }
            val octets = parts[4].split('.')
            if (octets.size != 4) return@mapNotNull null
            val numbers = octets.map { it.toIntOrNull() ?: return@mapNotNull null }
            if (numbers.any { it !in 0..255 }) return@mapNotNull null
            numbers.joinToString(".")
        }.toSet()

    private suspend fun installMediaCandidateRoutes(session: ComputingSession, answerSdp: String) {
        val expected = if (session.role == ComputeRole.producer) "a=recvonly" else "a=sendonly"
        if (answerSdp.lineSequence().none { it.trimEnd('\r') == expected }) {
            invalidMediaResponse("Sandbox Answer must contain $expected")
        }
        session.connectionParameters.videoCodec?.let { codec ->
            val wanted = codec.removePrefix("video/")
            val negotiated = answerSdp.lineSequence().mapNotNull { raw ->
                val line = raw.trimEnd('\r')
                if (!line.startsWith("a=rtpmap:")) null
                else line.substringAfter(' ', "").substringBefore('/').takeIf(String::isNotBlank)
            }.toSet()
            if (negotiated.none { it.equals(wanted, ignoreCase = true) }) {
                invalidMediaResponse(
                    "Sandbox Answer did not negotiate required video codec $codec",
                )
            }
        }
        val candidates = candidateIpv4s(answerSdp)
        if (candidates.isEmpty()) invalidMediaResponse("Sandbox Answer has no IPv4 ICE candidate")
        replaceComputeRoutes(session, candidates)
    }

    private suspend fun closeMediaRecord(
        sessionId: String,
        connectionId: String,
        timeoutSeconds: Double,
    ) {
        val record = computingMutex.withLock {
            computingMedia[sessionId]?.takeIf { it.mediaConnectionId == connectionId }
        }
        val session = record?.session
            ?: computingMutex.withLock { computingSessions[sessionId] }
            ?: return
        try {
            deleteMediaConnection(session, connectionId, timeoutSeconds)
            computingMutex.withLock {
                if (computingMedia[sessionId]?.mediaConnectionId == connectionId) {
                    computingMedia.remove(sessionId)
                }
            }
        } finally {
            if (computingMutex.withLock { sessionId !in computingClosing }) {
                replaceComputeRoutes(session)
            }
        }
    }

    private suspend fun deleteMediaConnection(
        session: ComputingSession,
        connectionId: String,
        timeoutSeconds: Double,
    ) {
        val encoded = URI(null, null, "/$connectionId", null).rawPath.removePrefix("/")
        val url = "${mediaConnectionsUrl(session).trimEnd('/')}/$encoded"
        val response = sandboxTransport.requestWithStatus(
            "DELETE", url, null, timeoutSeconds, session.networkBinding.ueIpv4,
        )
        if (response.statusCode != 204) {
            throw AgentSdkException(
                ErrorCode.MEDIA_NEGOTIATION_FAILED,
                "Sandbox returned HTTP ${response.statusCode} for media DELETE",
                retryable = response.statusCode >= 500,
            )
        }
    }

    private fun validateComputeRequest(
        request: ComputeSessionRequest,
        expectedType: ComputeRequestType,
    ) {
        requireReady()
        if (request.messageType != "COMPUTE_SESSION_REQUEST") {
            invalidCompute("message_type must be COMPUTE_SESSION_REQUEST", "message_type")
        }
        if (request.requestType != expectedType) {
            invalidCompute("request_type must be ${expectedType.name}", "request_type")
        }
        validateComputeRequestId(request.requestId, "request_id")
        if (expectedType == ComputeRequestType.CREATE) {
            val context = request.acnContext
                ?: invalidCompute("acn_context is required for CREATE", "acn_context")
            requireComputeString(context.groupId, "acn_context.group_id")
            requireComputeString(context.requesterAgentId, "acn_context.requester_agent_id")
            requireComputeString(context.targetAgentId, "acn_context.target_agent_id")
            request.uiLocale?.let { requireComputeString(it, "ui_locale") }
            if (request.computeServiceSessionId != null || request.targetRequestId != null) {
                invalidCompute("CREATE must not contain a target session or request")
            }
            when (request.inputFormat) {
                ComputeInputFormat.NATURAL_LANGUAGE -> {
                    requireComputeString(request.text, "text")
                    if (request.constraints != null) {
                        invalidCompute(
                            "Natural-language CREATE must not contain constraints",
                            "constraints",
                        )
                    }
                }
                ComputeInputFormat.STRUCTURED -> {
                    if (request.text != null) {
                        invalidCompute("Structured CREATE must not contain text", "text")
                    }
                    validateComputeConstraints(request.constraints)
                }
            }
            return
        }
        if (request.inputFormat != ComputeInputFormat.STRUCTURED) {
            invalidCompute(
                "QUERY, CANCEL and RELEASE require input_format=STRUCTURED",
                "input_format",
            )
        }
        if (
            request.acnContext != null || request.text != null ||
            request.constraints != null || request.uiLocale != null
        ) {
            invalidCompute("Non-CREATE requests must not contain CREATE fields")
        }
        val hasSession = request.computeServiceSessionId != null
        val hasTargetRequest = request.targetRequestId != null
        if (expectedType == ComputeRequestType.QUERY || expectedType == ComputeRequestType.CANCEL) {
            if (hasSession == hasTargetRequest) {
                invalidCompute(
                    "Exactly one of compute_service_session_id and target_request_id is required",
                )
            }
        } else if (!hasSession || hasTargetRequest) {
            invalidCompute(
                "RELEASE requires compute_service_session_id only",
                "compute_service_session_id",
            )
        }
        request.computeServiceSessionId?.let {
            requireComputeString(it, "compute_service_session_id")
        }
        request.targetRequestId?.let { validateComputeRequestId(it, "target_request_id") }
    }

    private fun validateComputeConstraints(constraints: ComputeConstraints?) {
        val value = constraints
            ?: invalidCompute("constraints are required for structured CREATE", "constraints")
        requireComputeString(value.capabilityId, "constraints.capability_id")
        listOf(
            "constraints.api_version" to value.apiVersion,
            "constraints.image_id" to value.imageId,
            "constraints.dnn" to value.dnn,
            "constraints.snssai" to value.snssai,
            "constraints.placement_region" to value.placementRegion,
            "constraints.data_residency_region" to value.dataResidencyRegion,
        ).forEach { (field, item) -> item?.let { requireComputeString(it, field) } }
        value.resources?.let { resources ->
            listOf(
                "cpu_millicores" to resources.cpuMillicores,
                "memory_mib" to resources.memoryMib,
                "gpu_count" to resources.gpuCount,
            ).forEach { (field, item) ->
                if (item != null && item !in 0..UINT32_MAX) {
                    invalidCompute(
                        "constraints.resources.$field must be uint32",
                        "constraints.resources.$field",
                    )
                }
            }
            resources.gpuModel?.let {
                requireComputeString(it, "constraints.resources.gpu_model")
            }
        }
        value.maxDurationMs?.let {
            if (it !in 0..UINT32_MAX) {
                invalidCompute("constraints.max_duration_ms must be uint32", "constraints.max_duration_ms")
            }
        }
    }

    private fun validateComputeRequestId(value: String, field: String) {
        if (value.toByteArray(Charsets.UTF_8).size !in 1..128) {
            invalidCompute("$field must contain 1..128 UTF-8 bytes", field)
        }
    }

    private fun invalidCompute(message: String, field: String? = null): Nothing =
        throw AgentSdkException(ErrorCode.INVALID_ARGUMENT, message, field)

    private fun requireComputeString(value: String?, field: String): String =
        value?.takeIf(String::isNotBlank)
            ?: invalidCompute("$field must be a non-empty string", field)

    private suspend fun computePreflight() {
        val runtime = checkNotNull(runtime)
        if (runtime.getAcnStatus()["ready"]?.jsonPrimitive?.booleanOrNull != true) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "GET /v1/acn/status did not report ready=true (nas_not_ready)",
                retryable = true,
            )
        }
        val snapshot = runtime.getUeInfo()
        selectDefaultUeAgentIp(snapshot)
        val sessions = snapshot["pdu_sessions"] as? JsonArray
        if (sessions == null || sessions.none { element ->
                val item = element as? JsonObject
                item?.stringOrNull("state") == "active" && item.stringOrNull("type") == "IPv4"
            }
        ) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "No active IPv4 PDU Session is available (pdu-session-required)",
            )
        }
        val accesses = snapshot["data_plane_accesses"] as? JsonArray
        if (accesses == null || accesses.none { element ->
                val item = element as? JsonObject
                item?.stringOrNull("access_type") == "HTTP3_CONNECT_IP" &&
                    item.stringOrNull("session_selection") == "EXACT_PDU_SESSION_ID"
            }
        ) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime provides no exact HTTP3 CONNECT-IP data-plane access " +
                    "(data-plane-access-unsupported)",
            )
        }
        ueInfo = snapshot
    }

    private suspend fun sendComputeRequest(
        request: ComputeSessionRequest,
        timeoutSeconds: Double,
    ): ComputeSessionStatus {
        if (timeoutSeconds <= 0.0) {
            invalidCompute("timeoutSeconds must be greater than zero", "timeoutSeconds")
        }
        val body = computeJson.encodeToJsonElement(request).jsonObject
        computingControlMutex.withLock {
            computingMutex.withLock {
                if (identityRemovalInProgress) {
                    throw AgentSdkException(
                        ErrorCode.AGENT_STATE_INVALID,
                        "Computing requests cannot start while the Agent Profile is being removed",
                    )
                }
                val previous = computeRequests[request.requestId]
                if (previous != null && previous != body) {
                    invalidCompute(
                        "request_id was already used with different computing content",
                        "request_id",
                    )
                }
                computeRequests[request.requestId] = body
                computingRequestsInFlight += request.requestId
            }
        }
        try {
            computePreflight()
            if (request.requestType == ComputeRequestType.CREATE) {
                computingMutex.withLock { computeCreateRequests[request.requestId] = request }
            }
            val activeRuntime = checkNotNull(runtime)
            val statusWaiter = CompletableDeferred<ComputeSessionStatus>()
            val knownStatus = computingMutex.withLock {
                computingStatusesByRequest[request.requestId] ?: run {
                    computingStatusWaiters.getOrPut(request.requestId) { mutableListOf() } += statusWaiter
                    null
                }
            }
            if (knownStatus != null) return knownStatus
            return try {
                withTimeout((timeoutSeconds * 1000).toLong()) {
                    coroutineScope {
                        val httpStatus = async {
                            val response = activeRuntime.requestWithStatus(
                                "POST",
                                COMPUTING_SESSION_REQUEST_PATH,
                                body,
                                timeoutSeconds,
                            )
                            parseComputeHttpResponse(request, response)
                        }
                        select {
                            statusWaiter.onAwait { status ->
                                httpStatus.cancel()
                                status
                            }
                            httpStatus.onAwait { it }
                        }
                    }
                }
            } finally {
                computingMutex.withLock {
                    computingStatusWaiters[request.requestId]?.let { waiters ->
                        waiters.remove(statusWaiter)
                        if (waiters.isEmpty()) computingStatusWaiters.remove(request.requestId)
                    }
                }
            }
        } finally {
            computingControlMutex.withLock {
                computingMutex.withLock { computingRequestsInFlight -= request.requestId }
            }
        }
    }

    private suspend fun parseComputeHttpResponse(
        request: ComputeSessionRequest,
        response: RuntimeHttpResponse,
    ): ComputeSessionStatus {
        if (response.body.stringOrNull("message_type") == COMPUTE_SESSION_STATUS) {
            val allowedStatus = when (request.requestType) {
                ComputeRequestType.CREATE -> setOf(202)
                ComputeRequestType.QUERY -> setOf(200)
                ComputeRequestType.CANCEL, ComputeRequestType.RELEASE -> setOf(200, 202)
            }
            if (response.statusCode !in allowedStatus && response.statusCode !in 400..499) {
                throw AgentSdkException(
                    ErrorCode.RUNTIME_REJECTED,
                    "Runtime returned invalid HTTP ${response.statusCode} for ${request.requestType}",
                )
            }
            val parsed = parseComputeStatus(response.body)
            if (parsed.requestId != request.requestId) {
                throw AgentSdkException(
                    ErrorCode.RUNTIME_REJECTED,
                    "C-04 request_id does not match the computing request",
                    "request_id",
                )
            }
            rememberComputeStatus(parsed)
            return computingMutex.withLock {
                computingStatusesByRequest[request.requestId] ?: parsed
            }
        }
        val error = response.body["error"] as? JsonObject
        if (error != null) {
            val code = error.stringOrNull("code") ?: "invalid-response"
            val message = error.stringOrNull("message")
                ?: "Runtime rejected computing request: $code"
            throw AgentSdkException(
                if (response.statusCode == 504) ErrorCode.TIMEOUT else ErrorCode.RUNTIME_REJECTED,
                message,
                retryable = response.statusCode in setOf(503, 504),
            )
        }
        throw AgentSdkException(
            ErrorCode.RUNTIME_REJECTED,
            "Runtime returned HTTP ${response.statusCode} without C-04 or error",
        )
    }

    private fun parseComputeStatus(
        payload: JsonObject,
        messageTypeInPayload: Boolean = true,
    ): ComputeSessionStatus {
        if (messageTypeInPayload && payload.stringOrNull("message_type") != COMPUTE_SESSION_STATUS) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "message_type must be COMPUTE_SESSION_STATUS",
                "message_type",
            )
        }
        val requestId = payload.requireRuntimeString("request_id")
        val status = payload.requireRuntimeString("status")
        if (status !in COMPUTE_STATUSES) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "status is not a defined computing session status",
                "status",
            )
        }
        val cause = payload.optionalRuntimeString("cause")
            ?: throw AgentSdkException(ErrorCode.RUNTIME_REJECTED, "cause must be a string", "cause")
        val sessionId = payload.optionalRuntimeString("compute_service_session_id")
        val revision = payload.optionalRuntimeString("status_revision")
        if (sessionId != null) {
            if (sessionId.isBlank() || revision?.toULongOrNull() == null) {
                throw AgentSdkException(
                    ErrorCode.RUNTIME_REJECTED,
                    "Existing sessions require a uint64 decimal status_revision",
                    "status_revision",
                )
            }
        } else if (revision != null) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "status_revision requires compute_service_session_id",
                "status_revision",
            )
        }
        val rawMissingFields = payload["missing_fields"]
        if (rawMissingFields != null && rawMissingFields !is JsonArray) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "missing_fields must be an array of strings",
                "missing_fields",
            )
        }
        val missingFields = (rawMissingFields as? JsonArray).orEmpty().map {
            (it as? JsonPrimitive)?.takeIf(JsonPrimitive::isString)
                ?.contentOrNull?.takeIf(String::isNotEmpty)
                ?: throw AgentSdkException(
                    ErrorCode.RUNTIME_REJECTED,
                    "missing_fields must contain strings",
                    "missing_fields",
                )
        }
        if (status == "CLARIFICATION_REQUIRED" && rawMissingFields == null) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "CLARIFICATION_REQUIRED requires missing_fields",
                "missing_fields",
            )
        }
        val result = payload["result"] as? JsonObject
        if (payload.containsKey("result") && result == null) {
            throw AgentSdkException(ErrorCode.RUNTIME_REJECTED, "result must be an object", "result")
        }
        return ComputeSessionStatus(
            messageType = COMPUTE_SESSION_STATUS,
            requestId = requestId,
            computeServiceSessionId = sessionId,
            statusRevision = revision,
            status = status,
            cause = cause,
            missingFields = missingFields,
            result = result,
        )
    }

    private suspend fun rememberComputeStatus(status: ComputeSessionStatus) {
        computingMutex.withLock {
            val sessionId = status.computeServiceSessionId
            val revision = status.statusRevision?.toULongOrNull()
            if (
                sessionId != null && sessionId in computingClosedSessionIds &&
                status.status !in COMPUTE_TERMINAL_STATUSES
            ) {
                Log.i(
                    TAG,
                    "Ignoring non-terminal computing status for locally closed " +
                        "session_id=$sessionId status=${status.status}",
                )
                return@withLock
            }
            if (sessionId != null && revision != null) {
                val currentRevision = computingStatuses[sessionId]?.statusRevision?.toULongOrNull()
                if (currentRevision != null && revision <= currentRevision) {
                    val currentForRequest = computingStatusesByRequest[status.requestId]
                    val requestRevision = currentForRequest?.statusRevision?.toULongOrNull()
                    if (
                        currentForRequest == null ||
                        currentForRequest.computeServiceSessionId != sessionId ||
                        requestRevision == null || revision > requestRevision
                    ) {
                        computingStatusesByRequest[status.requestId] = status
                        computingStatusWaiters.remove(status.requestId).orEmpty().forEach {
                            it.complete(status)
                        }
                    }
                    Log.i(
                        TAG,
                        "Ignoring stale computing status request_id=${status.requestId} " +
                            "session_id=$sessionId status_revision=$revision " +
                            "current_revision=$currentRevision",
                    )
                    return@withLock
                }
                computingStatuses[sessionId] = status
                if (status.status in COMPUTE_TERMINAL_STATUSES) {
                    computingWaiters.remove(sessionId).orEmpty().forEach {
                        it.completeExceptionally(terminalComputeException(status))
                    }
                }
            }
            computingStatusesByRequest[status.requestId] = status
            computingStatusWaiters.remove(status.requestId).orEmpty().forEach {
                it.complete(status)
            }
            Log.i(TAG, computeStatusDiagnostic(status))
        }
    }

    private fun computeStatusDiagnostic(status: ComputeSessionStatus): String = buildString {
        append("Computing session status")
        append(" request_id=${status.requestId}")
        append(" session_id=${status.computeServiceSessionId ?: "<none>"}")
        append(" status_revision=${status.statusRevision ?: "<none>"}")
        append(" status=${status.status}")
        append(" cause=${status.cause.ifBlank { "<none>" }}")
        if (status.missingFields.isNotEmpty()) {
            append(" missing_fields=${status.missingFields}")
        }
        status.result?.let {
            append(" result=${it.toString().take(COMPUTE_STATUS_RESULT_LOG_LIMIT)}")
        }
    }

    private fun terminalComputeException(status: ComputeSessionStatus): AgentSdkException =
        AgentSdkException(
            ErrorCode.COMPUTING_SESSION_INVALID,
            buildString {
                append("Computing session ended in state ${status.status}")
                append(" (cause=${status.cause.ifBlank { "<none>" }}")
                append(", status_revision=${status.statusRevision ?: "<none>"}")
                status.result?.let {
                    append(", result=${it.toString().take(COMPUTE_STATUS_RESULT_LOG_LIMIT)}")
                }
                append(')')
            },
        )

    private suspend fun handleComputeSessionStatus(payload: JsonObject) {
        rememberComputeStatus(parseComputeStatus(payload, messageTypeInPayload = false))
    }

    private fun parseComputeConnectConfig(payload: JsonObject): ComputingSession {
        val sessionId = payload.requireRuntimeString("compute_service_session_id")
        val instanceId = payload.requireRuntimeString("compute_instance_id")
        val bindingRef = payload.requireRuntimeString("binding_ref")
        val role = try {
            ComputeRole.valueOf(payload.requireRuntimeString("role"))
        } catch (error: Exception) {
            throw AgentSdkException(ErrorCode.RUNTIME_REJECTED, "Invalid role", "role", cause = error)
        }
        val receiverAgentId = payload.requireRuntimeString("receiver_agent_id")
        val serviceEndpoint = requireServiceEndpoint(
            payload.stringOrNull("service_endpoint"),
            "service_endpoint",
        )
        val rawBinding = payload["network_binding"] as? JsonObject
            ?: computePayloadError("network_binding must be an object", "network_binding")
        val rawSnssai = rawBinding["snssai"] as? JsonObject
            ?: computePayloadError("network_binding.snssai must be an object", "network_binding.snssai")
        val rawDataPlane = rawBinding["runtime_data_plane"] as? JsonObject
            ?: computePayloadError(
                "network_binding.runtime_data_plane must be an object",
                "network_binding.runtime_data_plane",
            )
        val binding = ComputeNetworkBinding(
            pduSessionId = requireUnsignedInt(
                rawBinding["pdu_session_id"]?.jsonPrimitive?.intOrNull,
                "network_binding.pdu_session_id",
                255,
            ),
            dnn = rawBinding.requireRuntimeString("dnn"),
            snssai = Snssai(
                sst = requireUnsignedInt(
                    rawSnssai["sst"]?.jsonPrimitive?.intOrNull,
                    "network_binding.snssai.sst",
                    255,
                ),
                sd = parseOptionalSd(rawSnssai.optionalRuntimeString("sd")),
            ),
            ueIpv4 = normalizeIpv4(
                rawBinding.stringOrNull("ue_ipv4"),
                "network_binding.ue_ipv4",
            ),
            runtimeDataPlane = RuntimeDataPlane(
                accessType = rawDataPlane.requireRuntimeString("access_type"),
                sessionSelection = rawDataPlane.requireRuntimeString("session_selection"),
            ),
        )
        val rawParameters = payload["connection_parameters"] as? JsonObject
            ?: computePayloadError(
                "connection_parameters must be an object",
                "connection_parameters",
            )
        val parameters = ComputeConnectionParameters(
            mediaConnectionsPath = requireAbsolutePath(
                rawParameters.stringOrNull("media_connections_path"),
                "connection_parameters.media_connections_path",
            ),
            transport = rawParameters.requireRuntimeString("transport"),
            recognitionTargetPathTemplate = rawParameters
                .optionalRuntimeString("recognition_target_path_template")
                ?.let {
                    requireAbsolutePath(it, "connection_parameters.recognition_target_path_template")
                },
            videoCodec = rawParameters.optionalRuntimeString("video_codec")?.let {
                it.takeIf(String::isNotBlank)
                    ?: computePayloadError(
                        "connection_parameters.video_codec must be a non-empty string",
                        "connection_parameters.video_codec",
                    )
            },
        )
        if (parameters.transport != "WEBRTC") {
            computePayloadError(
                "connection_parameters.transport must be WEBRTC",
                "connection_parameters.transport",
            )
        }
        parameters.recognitionTargetPathTemplate?.let { template ->
            val marker = "{compute_service_session_id}"
            if (template.windowed(marker.length).count { it == marker } != 1) {
                computePayloadError(
                    "recognition_target_path_template must contain " +
                        "{compute_service_session_id} exactly once",
                    "connection_parameters.recognition_target_path_template",
                )
            }
        }
        val expiresAt = payload.optionalRuntimeString("expires_at")?.let {
            try {
                Instant.parse(it)
            } catch (error: Exception) {
                computePayloadError("expires_at must be RFC3339", "expires_at", error)
            }
        }
        return ComputingSession(
            computeServiceSessionId = sessionId,
            computeInstanceId = instanceId,
            bindingRef = bindingRef,
            role = role,
            receiverAgentId = receiverAgentId,
            serviceEndpoint = serviceEndpoint,
            networkBinding = binding,
            connectionParameters = parameters,
            expiresAt = expiresAt,
        )
    }

    private suspend fun handleComputeConnectConfig(payload: JsonObject): JsonObject {
        val session = try {
            parseComputeConnectConfig(payload)
        } catch (_: AgentSdkException) {
            return rawComputeConfigAck(payload, false, "invalid-request")
        }
        val key = Triple(session.bindingRef, session.role.name, session.receiverAgentId)
        computingMutex.withLock {
            if (identityRemovalInProgress) {
                return computeConfigAck(session, false, "session-closed")
            }
            if (session.computeServiceSessionId in computingClosedSessionIds) {
                return computeConfigAck(session, false, "session-closed")
            }
            if (computingCloseResults.containsKey(key)) {
                return computeConfigAck(session, false, "session-closed")
            }
            if (
                computingStatuses[session.computeServiceSessionId]
                    ?.status in COMPUTE_TERMINAL_STATUSES
            ) {
                return computeConfigAck(session, false, "session-closed")
            }
            computingSessions[session.computeServiceSessionId]?.let { current ->
                return if (current == session) {
                    computeConfigAck(session, true, "")
                } else {
                    computeConfigAck(session, false, "config-conflict")
                }
            }
            if (computingSessions.values.any {
                    it.bindingRef == session.bindingRef &&
                        it.role == session.role &&
                        it.receiverAgentId == session.receiverAgentId
                }
            ) {
                return computeConfigAck(session, false, "config-conflict")
            }
        }
        try {
            validateAndInstallComputeBinding(session)
        } catch (error: ComputeBindingException) {
            return computeConfigAck(session, false, error.protocolCause)
        }
        val rejectionAfterInstall = computingMutex.withLock {
            val rejection = when {
                identityRemovalInProgress || profile?.agentId != session.receiverAgentId ->
                    "session-closed"
                computingStatuses[session.computeServiceSessionId]
                    ?.status in COMPUTE_TERMINAL_STATUSES -> "session-closed"
                else -> null
            }
            if (rejection == null) {
                computingSessions[session.computeServiceSessionId] = session
                computingWaiters.remove(session.computeServiceSessionId).orEmpty().forEach {
                    it.complete(session)
                }
            }
            rejection
        }
        if (rejectionAfterInstall != null) {
            runCatching {
                tunnelController.replaceGroupPeers(
                    computingRouteKey(session.bindingRef),
                    emptySet(),
                )
            }
            return computeConfigAck(session, false, rejectionAfterInstall)
        }
        return computeConfigAck(session, true, "")
    }

    private suspend fun validateAndInstallComputeBinding(session: ComputingSession) {
        if (profile == null || session.receiverAgentId != profile!!.agentId) {
            throw ComputeBindingException("binding-mismatch")
        }
        val binding = session.networkBinding
        if (
            binding.runtimeDataPlane.accessType != "HTTP3_CONNECT_IP" ||
            binding.runtimeDataPlane.sessionSelection != "EXACT_PDU_SESSION_ID"
        ) {
            throw ComputeBindingException("data-plane-access-unsupported")
        }
        val snapshot = ueInfo ?: checkNotNull(runtime).getUeInfo().also { ueInfo = it }
        val sessions = snapshot["pdu_sessions"] as? JsonArray
        val matching = sessions.orEmpty().mapNotNull { it as? JsonObject }.filter {
            it["pdu_session_id"]?.jsonPrimitive?.intOrNull == binding.pduSessionId &&
                it.stringOrNull("state") == "active" && it.stringOrNull("type") == "IPv4"
        }
        if (matching.size != 1) throw ComputeBindingException("pdu-session-not-found")
        val pdu = matching.single()
        val localSnssai = pdu["snssai"] as? JsonObject
        val localSst = localSnssai?.get("sst")?.jsonPrimitive?.intOrNull
        val localSd = localSnssai?.stringOrNull("sd")
        val localIp = try {
            normalizeIpv4(pdu.stringOrNull("ipv4"), "pdu_sessions.ipv4")
        } catch (_: AgentSdkException) {
            throw ComputeBindingException("network-binding-mismatch")
        }
        if (
            pdu.stringOrNull("dnn") != binding.dnn ||
            localSst != binding.snssai.sst || localSd != binding.snssai.sd ||
            localIp != binding.ueIpv4 || agentTunIp != binding.ueIpv4
        ) {
            throw ComputeBindingException("network-binding-mismatch")
        }
        val accesses = snapshot["data_plane_accesses"] as? JsonArray
        val matchingAccesses = accesses.orEmpty().mapNotNull { it as? JsonObject }.filter {
            it.stringOrNull("access_type") == binding.runtimeDataPlane.accessType &&
                it.stringOrNull("session_selection") == binding.runtimeDataPlane.sessionSelection
        }
        if (matchingAccesses.size != 1) {
            throw ComputeBindingException("data-plane-access-unsupported")
        }
        val template = matchingAccesses.single().stringOrNull("endpoint_template")
        if (template == null || template.windowed("{pdu_session_id}".length)
                .count { it == "{pdu_session_id}" } != 1
        ) {
            throw ComputeBindingException("data-plane-access-unsupported")
        }
        val expanded = template.replace("{pdu_session_id}", binding.pduSessionId.toString())
        val accessUri = runCatching { URI(expanded) }.getOrNull()
        if (
            accessUri?.scheme != "https" || accessUri.host.isNullOrBlank() ||
            !masqueTransport.connected
        ) {
            throw ComputeBindingException("data-plane-access-unsupported")
        }
        installComputeRoute(session)
    }

    private suspend fun installComputeRoute(session: ComputingSession) {
        replaceComputeRoutes(session)
    }

    private suspend fun replaceComputeRoutes(
        session: ComputingSession,
        extraAddresses: Set<String> = emptySet(),
    ) {
        val host = URI(session.serviceEndpoint).host ?: throw ComputeBindingException("config-rejected")
        val addresses = withContext(Dispatchers.IO) {
            InetAddress.getAllByName(host).filter { it.address.size == 4 }.mapNotNull { it.hostAddress }.toSet()
        } + extraAddresses
        if (addresses.isEmpty()) throw ComputeBindingException("config-rejected")
        tunnelController.replaceGroupPeers(computingRouteKey(session.bindingRef), addresses)
    }

    private fun computeConfigAck(
        session: ComputingSession,
        accepted: Boolean,
        cause: String,
    ): JsonObject = buildJsonObject {
        put("compute_service_session_id", session.computeServiceSessionId)
        put("compute_instance_id", session.computeInstanceId)
        put("binding_ref", session.bindingRef)
        put("role", session.role.name)
        put("receiver_agent_id", session.receiverAgentId)
        put("network_binding", networkBindingJson(session.networkBinding))
        put("accepted", accepted)
        put("cause", cause)
    }

    private fun rawComputeConfigAck(
        payload: JsonObject,
        accepted: Boolean,
        cause: String,
    ): JsonObject = buildJsonObject {
        listOf(
            "compute_service_session_id",
            "compute_instance_id",
            "binding_ref",
            "role",
            "receiver_agent_id",
            "network_binding",
        ).forEach { field -> payload[field]?.let { put(field, it) } }
        put("accepted", accepted)
        put("cause", cause)
    }

    private fun networkBindingJson(binding: ComputeNetworkBinding): JsonObject = buildJsonObject {
        put("pdu_session_id", binding.pduSessionId)
        put("dnn", binding.dnn)
        put("snssai", buildJsonObject {
            put("sst", binding.snssai.sst)
            binding.snssai.sd?.let { put("sd", it) }
        })
        put("ue_ipv4", binding.ueIpv4)
        put("runtime_data_plane", buildJsonObject {
            put("access_type", binding.runtimeDataPlane.accessType)
            put("session_selection", binding.runtimeDataPlane.sessionSelection)
        })
    }

    private suspend fun handleComputeSessionClose(payload: JsonObject): JsonObject {
        val fields = listOf(
            "compute_service_session_id",
            "compute_instance_id",
            "binding_ref",
            "role",
            "receiver_agent_id",
        )
        val values = try {
            fields.associateWith { field -> payload.requireRuntimeString(field) }
        } catch (_: AgentSdkException) {
            return buildJsonObject {
                fields.forEach { field ->
                    payload.stringOrNull(field)?.takeIf(String::isNotBlank)?.let { put(field, it) }
                }
                put("closed", false)
                put("cause", "invalid-request")
            }
        }
        val sessionId = values.getValue("compute_service_session_id")
        val instanceId = values.getValue("compute_instance_id")
        val bindingRef = values.getValue("binding_ref")
        val role = values.getValue("role")
        val receiverAgentId = values.getValue("receiver_agent_id")
        val hasCause = try {
            payload.optionalRuntimeString("cause") != null
        } catch (_: AgentSdkException) {
            false
        }
        if (!hasCause) {
            return closeAck(sessionId, instanceId, bindingRef, role, receiverAgentId, false, "invalid-request")
        }
        val key = Triple(bindingRef, role, receiverAgentId)
        computingMutex.withLock { computingCloseResults[key]?.let { return it } }
        val session = computingMutex.withLock { computingSessions[sessionId] }
        if (
            session == null || session.computeInstanceId != instanceId ||
            session.bindingRef != bindingRef || session.role.name != role ||
            session.receiverAgentId != receiverAgentId
        ) {
            return closeAck(
                sessionId, instanceId, bindingRef, role, receiverAgentId,
                false, "binding-mismatch",
            )
        }
        computingMutex.withLock { computingClosing += sessionId }
        val result = try {
            computingMutex.withLock { computingPendingMedia.remove(sessionId) }
                ?.let { pending -> runCatching { pending.prepared.abort() } }
            runCatching {
                when (val managed = computingMutex.withLock { computingMedia[sessionId]?.managed }) {
                    is VideoUploadHandle -> managed.stop()
                    is ProcessedVideoStream -> managed.close()
                }
            }
            computingMutex.withLock { computingMedia.remove(sessionId) }
            tunnelController.replaceGroupPeers(computingRouteKey(bindingRef), emptySet())
            computingMutex.withLock { computingSessions.remove(sessionId) }
            closeAck(sessionId, instanceId, bindingRef, role, receiverAgentId, true, "")
        } catch (_: Exception) {
            closeAck(
                sessionId, instanceId, bindingRef, role, receiverAgentId,
                false, "runtime-unhealthy",
            )
        }
        computingMutex.withLock {
            computingCloseResults[key] = result
            if (result["closed"]?.jsonPrimitive?.booleanOrNull == true) {
                computingClosedSessionIds += sessionId
                computingCloseWaiters.remove(sessionId).orEmpty().forEach { it.complete(Unit) }
            } else {
                val failure = AgentSdkException(
                    ErrorCode.COMPUTING_SESSION_INVALID,
                    "C-05 failed to close computing session $sessionId " +
                        "(cause=${result.stringOrNull("cause") ?: "runtime-unhealthy"})",
                )
                computingCloseWaiters.remove(sessionId).orEmpty().forEach {
                    it.completeExceptionally(failure)
                }
            }
        }
        return result
    }

    private fun closeAck(
        sessionId: String,
        instanceId: String,
        bindingRef: String,
        role: String,
        receiverAgentId: String,
        closed: Boolean,
        cause: String,
    ): JsonObject = buildJsonObject {
        put("compute_service_session_id", sessionId)
        put("compute_instance_id", instanceId)
        put("binding_ref", bindingRef)
        put("role", role)
        put("receiver_agent_id", receiverAgentId)
        put("closed", closed)
        put("cause", cause)
    }

    private suspend fun waitForComputingSession(
        sessionId: String,
        timeoutSeconds: Double,
    ): ComputingSession {
        requireComputeString(sessionId, "compute_service_session_id")
        val waiter = computingMutex.withLock {
            computingStatuses[sessionId]?.takeIf { it.status in COMPUTE_TERMINAL_STATUSES }?.let {
                throw terminalComputeException(it)
            }
            computingSessions[sessionId]?.let { return it }
            CompletableDeferred<ComputingSession>().also {
                computingWaiters.getOrPut(sessionId) { mutableListOf() } += it
            }
        }
        return try {
            withTimeout((timeoutSeconds * 1000).toLong()) { waiter.await() }
        } finally {
            computingMutex.withLock { computingWaiters[sessionId]?.remove(waiter) }
        }
    }

    private suspend fun recoverComputeStatuses() {
        val creates = computingMutex.withLock { computeCreateRequests.values.toList() }
        creates.forEach { create ->
            val known = computingMutex.withLock {
                computingStatusesByRequest[create.requestId]?.let { byRequest ->
                    byRequest.computeServiceSessionId?.let { computingStatuses[it] }
                        ?: byRequest
                }
            }
            if (known?.status in COMPUTE_TERMINAL_STATUSES) return@forEach
            val query = ComputeSessionRequest(
                messageType = "COMPUTE_SESSION_REQUEST",
                requestType = ComputeRequestType.QUERY,
                inputFormat = ComputeInputFormat.STRUCTURED,
                requestId = UUID.randomUUID().toString(),
                targetRequestId = create.requestId,
            )
            runCatching { sendComputeRequest(query, 30.0) }
                .onFailure { Log.w(TAG, "Computing status recovery failed", it) }
        }
    }

    private fun selectDefaultUeAgentIp(payload: JsonObject): String {
        val nas = payload["nas"] as? JsonObject
            ?: throw AgentSdkException(ErrorCode.RUNTIME_REJECTED, "GET /v1/ue/info has no nas")
        if (
            nas["registered"]?.jsonPrimitive?.booleanOrNull != true ||
            nas.stringOrNull("state") != "session_ready" ||
            nas["security_context"]?.jsonPrimitive?.booleanOrNull != true
        ) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "GET /v1/ue/info reports NAS is not ready",
                retryable = true,
            )
        }
        val sessions = payload["pdu_sessions"] as? JsonArray
            ?: throw AgentSdkException(ErrorCode.RUNTIME_REJECTED, "pdu_sessions is required")
        val defaults = sessions.mapNotNull { it as? JsonObject }.filter {
            it.stringOrNull("state") == "active" && it.stringOrNull("type") == "IPv4" &&
                it["default_route"]?.jsonPrimitive?.booleanOrNull == true
        }.map { normalizeIpv4(it.stringOrNull("ipv4"), "pdu_sessions.ipv4") }
        if (defaults.size != 1) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "Exactly one active default IPv4 PDU Session is required",
                "pdu_sessions",
                retryable = true,
            )
        }
        return defaults.single()
    }

    private fun normalizeIpv4(value: String?, field: String): String {
        val text = value?.takeIf(String::isNotBlank)
            ?: computePayloadError("$field must be an IPv4 literal", field)
        val address = try {
            InetAddress.getByName(text)
        } catch (error: Exception) {
            computePayloadError("$field must be an IPv4 literal", field, error)
        }
        if (address.address.size != 4) computePayloadError("$field must be an IPv4 literal", field)
        return address.hostAddress ?: computePayloadError("$field is invalid", field)
    }

    private fun requireServiceEndpoint(value: String?, field: String): String {
        val text = value?.takeIf(String::isNotBlank)
            ?: computePayloadError("$field must be a non-empty string", field)
        val uri = try {
            URI(text)
        } catch (error: Exception) {
            computePayloadError("$field must be an absolute HTTP or HTTPS URI", field, error)
        }
        if (
            uri.scheme !in setOf("http", "https") || uri.host.isNullOrBlank() ||
            uri.userInfo != null || uri.fragment != null
        ) {
            computePayloadError("$field must be an absolute HTTP or HTTPS URI", field)
        }
        return text.trimEnd('/')
    }

    private fun requireAbsolutePath(value: String?, field: String): String {
        val text = value?.takeIf(String::isNotBlank)
            ?: computePayloadError("$field must be a non-empty path", field)
        val uri = runCatching { URI(text) }.getOrNull()
        if (!text.startsWith('/') || uri?.isAbsolute == true || uri?.host != null) {
            computePayloadError("$field must be an absolute path", field)
        }
        return text
    }

    private fun parseOptionalSd(value: String?): String? {
        if (value == null) return null
        if (!Regex("[0-9A-Fa-f]{6}").matches(value)) {
            computePayloadError(
                "network_binding.snssai.sd must be six hexadecimal characters",
                "network_binding.snssai.sd",
            )
        }
        return value
    }

    private fun requireUnsignedInt(value: Int?, field: String, maximum: Int): Int {
        if (value == null || value !in 0..maximum) computePayloadError("$field is invalid", field)
        return value
    }

    private fun computePayloadError(
        message: String,
        field: String,
        cause: Throwable? = null,
    ): Nothing = throw AgentSdkException(ErrorCode.RUNTIME_REJECTED, message, field, cause = cause)

    private fun computingRouteKey(bindingRef: String): String = "computing:$bindingRef"

    private fun mediaConnectionsUrl(session: ComputingSession): String =
        URI(session.serviceEndpoint.trimEnd('/') + "/")
            .resolve(session.connectionParameters.mediaConnectionsPath)
            .toString()

    private fun recognitionTargetUrl(session: ComputingSession): String {
        val template = session.connectionParameters.recognitionTargetPathTemplate
            ?: throw AgentSdkException(
                ErrorCode.COMPUTING_SESSION_INVALID,
                "C-02 does not provide recognition_target_path_template",
                "connection_parameters.recognition_target_path_template",
            )
        val encodedSessionId = URI(null, null, "/${session.computeServiceSessionId}", null)
            .rawPath.removePrefix("/")
        val path = template.replace("{compute_service_session_id}", encodedSessionId)
        return URI(session.serviceEndpoint.trimEnd('/') + "/").resolve(path).toString()
    }

    private fun controlActionsUrl(session: ComputingSession): String =
        URI(session.serviceEndpoint.trimEnd('/') + "/")
            .resolve("/v1/control-actions")
            .toString()

    private fun audioControlActionsUrl(session: ComputingSession): String =
        URI(session.serviceEndpoint.trimEnd('/') + "/")
            .resolve("/v1/audio-control-actions")
            .toString()

    private class ComputeBindingException(val protocolCause: String) : RuntimeException(protocolCause)

    suspend fun getGroupSnapshot(groupId: String): GroupConfigSnapshot? =
        groupCache?.snapshot(groupId)

    fun getMasqueTransportStatistics(): MasqueTransportStatistics =
        masqueTransport.statistics()

    suspend fun close() {
        if (state == State.CLOSED || state == State.CLOSING) return
        state = State.CLOSING
        computingMutex.withLock {
            computingClosing += computingSessions.keys
            computingClosing += computingPendingMedia.keys
        }
        computingMutex.withLock {
            computingPendingMedia.values.toList().also { computingPendingMedia.clear() }
        }.forEach { pending -> runCatching { pending.prepared.abort() } }
        computingMutex.withLock { computingMedia.values.toList() }.forEach { record ->
            runCatching {
                when (val managed = record.managed) {
                    is VideoUploadHandle -> managed.stop()
                    is ProcessedVideoStream -> managed.close()
                }
            }
        }
        runCatching { groupCache?.close() }
        runCatching { masqueTransport.close() }
        runCatching { localServer?.close() }
        runCatching { runtime?.close() }
        runCatching { sandboxTransport.close() }
        runCatching { tunnelController.close() }
        runCatching { mediaOffloadAdapter?.close() }
        computingMutex.withLock {
            val closed = AgentSdkException(
                ErrorCode.SDK_NOT_INITIALIZED,
                "SDK is closed",
            )
            computingWaiters.values.flatten().forEach { it.completeExceptionally(closed) }
            computingWaiters.clear()
            computingStatusWaiters.values.flatten().forEach { it.completeExceptionally(closed) }
            computingStatusWaiters.clear()
            computingCloseWaiters.values.flatten().forEach { it.completeExceptionally(closed) }
            computingCloseWaiters.clear()
            computingRequestsInFlight.clear()
            computingClosedSessionIds.clear()
            computeRequests.clear()
            computeCreateRequests.clear()
            computingStatuses.clear()
            computingStatusesByRequest.clear()
            computingSessions.clear()
            computingMedia.clear()
            computingPendingMedia.clear()
            computingMediaLocks.clear()
            computingClosing.clear()
            computingCloseResults.clear()
            identityRemovalInProgress = false
        }
        receivedA2aMutex.withLock { receivedA2aMessageIds.clear() }
        ueInfo = null
        state = State.CLOSED
    }

    private suspend fun rebindLocalServerAfterTunReplacement() {
        val previous = localServer ?: return
        Log.i(
            TAG,
            "Rebinding local ingress after TUN replacement " +
                "tcp=$agentTunIp:$localTcpPort udp=$agentTunIp:$localUdpPort",
        )
        localServer = null
        previous.close()
        var lastError: Exception? = null
        for ((attempt, retryDelayMs) in LOCAL_SERVER_REBIND_DELAYS_MS.withIndex()) {
            if (retryDelayMs > 0) delay(retryDelayMs)
            val replacement = localServerFactory()
            try {
                replacement.start(
                    agentIp = agentTunIp,
                    tcpPort = localTcpPort,
                    udpPort = localUdpPort,
                    onA2aMessage = ::handleA2aMessage,
                )
                localServer = replacement
                Log.i(TAG, "Local ingress rebound after TUN replacement attempt=${attempt + 1}")
                return
            } catch (error: Exception) {
                runCatching { replacement.close() }
                lastError = error
                if (attempt < LOCAL_SERVER_REBIND_DELAYS_MS.lastIndex) {
                    Log.w(
                        TAG,
                        "Local ingress rebind attempt=${attempt + 1} failed; retrying",
                        error,
                    )
                }
            }
        }
        Log.e(TAG, "Local ingress rebind failed after TUN replacement", lastError)
        throw checkNotNull(lastError)
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
        (this[field] as? JsonPrimitive)?.takeIf(JsonPrimitive::isString)?.contentOrNull

    private fun JsonObject.optionalRuntimeString(field: String): String? {
        val value = this[field] ?: return null
        return (value as? JsonPrimitive)?.takeIf(JsonPrimitive::isString)?.contentOrNull
            ?: throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime response field $field must be a string",
                field,
            )
    }

    private fun JsonObject.requireRuntimeString(field: String): String =
        stringOrNull(field)?.takeIf { it.isNotBlank() } ?: throw AgentSdkException(
            ErrorCode.RUNTIME_REJECTED,
            "Runtime response field $field must be a non-empty string",
            field,
        )

    private fun requireMediaAdapter(): MediaOffloadAdapter =
        mediaOffloadAdapter ?: throw AgentSdkException(
            ErrorCode.COMPUTING_SESSION_NOT_FOUND,
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
        private const val TAG = "AgentSdk"
        private const val COMPUTING_SESSION_REQUEST_PATH = "/v1/computing/session-requests"
        private const val COMPUTE_CONNECT_CONFIG = "COMPUTE_CONNECT_CONFIG"
        private const val COMPUTE_SESSION_STATUS = "COMPUTE_SESSION_STATUS"
        private const val COMPUTE_SESSION_CLOSE = "COMPUTE_SESSION_CLOSE"
        private const val COMPUTE_STATUS_RESULT_LOG_LIMIT = 2_000
        private const val RECEIVED_A2A_MESSAGE_CACHE_LIMIT = 1_024
        private val CONTROL_ACTION_STATUSES = setOf(
            "ACCEPTED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "UNKNOWN",
        )
        private val SUPPORTED_AUDIO_SUFFIXES = setOf("wav", "mp3", "m4a", "flac", "ogg", "webm")
        private const val MAX_AUDIO_UPLOAD_BYTES = 50 * 1024 * 1024
        private const val UINT32_MAX = 4_294_967_295L
        private val COMPUTE_TERMINAL_STATUSES = setOf(
            "REJECTED",
            "CLARIFICATION_REQUIRED",
            "REQUEST_CANCELLED",
            "NOT_FOUND",
            "FAILED",
            "COMPLETED",
        )
        private val COMPUTE_STATUSES = setOf(
            "ACCEPTED",
            "WAITING_PARTICIPANTS",
            "PLANNING",
            "COORDINATING",
            "RESERVED",
            "ACTIVATING",
            "MEDIA_CONNECTING",
            "ACTIVE",
            "RELEASING",
            "COMPENSATING",
            "CLEANUP_FAILED",
            "FAILED",
            "COMPLETED",
            "REJECTED",
            "CLARIFICATION_REQUIRED",
            "REQUEST_CANCELLED",
            "NOT_FOUND",
        )
        private val LOCAL_SERVER_REBIND_DELAYS_MS = longArrayOf(0, 25, 100, 250)

        fun create(
            vpnService: AgentVpnService,
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
                mediaOffloadAdapter = AndroidWebRtcMediaAdapter(vpnService),
                peerMessenger = peerMessenger,
                localServerFactory = localServerFactory,
            )
        }
    }
}
