package com.rayneo.agent.sdk.group

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.model.GroupConfigSnapshot
import com.rayneo.agent.sdk.model.GroupConfigWire
import com.rayneo.agent.sdk.model.GroupMemberInfo
import com.rayneo.agent.sdk.transport.TunnelController
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import java.net.InetAddress
import java.net.URI
import java.time.Instant
import java.util.Collections

class GroupMemberCache(
    private val tunnelController: TunnelController,
    private val json: Json = Json { ignoreUnknownKeys = false },
) {
    private val mutex = Mutex()
    private val snapshots = mutableMapOf<String, GroupConfigSnapshot>()

    fun buildCandidate(
        payload: JsonObject,
        localAgentId: String,
        localAgentIp: String,
        localTcpPort: Int,
        localUdpPort: Int,
    ): GroupConfigSnapshot {
        val wire = try {
            json.decodeFromJsonElement<GroupConfigWire>(payload)
        } catch (error: Exception) {
            throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_INVALID,
                "Invalid acf_group_config JSON: ${error.message}",
                cause = error,
            )
        }
        if (wire.notificationType != "acf_group_config") {
            throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_INVALID,
                "notification_type must be acf_group_config",
                "notification_type",
            )
        }
        if (!SEMANTIC_VERSION.matches(wire.version)) {
            throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_INVALID,
                "version must use semantic version syntax",
                "version",
            )
        }
        val major = wire.version.substringBefore('.').toIntOrNull()
            ?: throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_INVALID,
                "version must use semantic version syntax",
                "version",
            )
        if (major != 1) {
            throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_VERSION_UNSUPPORTED,
                "Unsupported group config version ${wire.version}",
                "version",
            )
        }
        val timestamp = try {
            Instant.parse(wire.timestamp)
        } catch (error: Exception) {
            throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_INVALID,
                "timestamp must be UTC RFC3339",
                "timestamp",
                cause = error,
            )
        }
        if (wire.groupId.isBlank() || wire.members.isEmpty()) {
            throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_INVALID,
                "group_id and members are required",
            )
        }
        if (wire.targetAgentId != null && wire.targetAgentId != localAgentId) {
            throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_INVALID,
                "target_agent_id does not match the local agent",
                "target_agent_id",
            )
        }

        val byId = linkedMapOf<String, GroupMemberInfo>()
        val ipOwners = mutableMapOf<String, String>()
        wire.members.forEach { (label, member) ->
            if (member.agentId.isBlank() || byId.containsKey(member.agentId)) {
                throw AgentSdkException(
                    ErrorCode.GROUP_CONFIG_INVALID,
                    "members.$label has a blank or duplicate agent_id",
                    "members.$label.agent_id",
                )
            }
            val normalizedIp = normalizeLiteralIp(member.agentIp, "members.$label.agent_ip")
            val previousOwner = ipOwners.putIfAbsent(normalizedIp, member.agentId)
            if (previousOwner != null && previousOwner != member.agentId) {
                throw AgentSdkException(
                    ErrorCode.GROUP_CONFIG_INVALID,
                    "Agent IP $normalizedIp is claimed by multiple members",
                    "members.$label.agent_ip",
                )
            }
            val (serviceEndpoint, tcpPort) = parseServiceEndpoint(
                member.serviceEndpoints,
                normalizedIp,
                "members.$label.service_endpoints",
            )
            if (
                member.agentName.isBlank() ||
                member.skills.any { it.isBlank() }
            ) {
                throw AgentSdkException(
                    ErrorCode.GROUP_CONFIG_INVALID,
                    "members.$label contains an empty required field",
                    "members.$label",
                )
            }
            byId[member.agentId] = GroupMemberInfo(
                agentId = member.agentId,
                agentName = member.agentName,
                capabilities = Collections.unmodifiableList(member.skills.toList()),
                agentIp = normalizedIp,
                tcpPort = tcpPort,
                udpPort = 0,
                didKey = "",
                serviceEndpoint = serviceEndpoint,
            )
        }

        val local = byId[localAgentId] ?: throw AgentSdkException(
            ErrorCode.GROUP_CONFIG_INVALID,
            "Group config does not contain the local agent",
            "members",
        )
        if (local.agentIp != normalizeLiteralIp(localAgentIp, "agent_tun_cidr")) {
            throw AgentSdkException(
                ErrorCode.AGENT_IP_MISMATCH,
                "Local member IP does not match the Agent TUN address",
                "members.agent_ip",
            )
        }
        if (local.tcpPort != localTcpPort) {
            throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_INVALID,
                "Local member service endpoint port does not match the SDK listener",
                "members",
            )
        }
        return GroupConfigSnapshot(
            groupId = wire.groupId,
            version = wire.version,
            notificationTimestamp = timestamp,
            membersByAgentId = Collections.unmodifiableMap(LinkedHashMap(byId)),
            generation = 0,
        )
    }

    suspend fun commit(
        candidate: GroupConfigSnapshot,
        localAgentId: String,
    ): GroupConfigSnapshot = mutex.withLock {
        val current = snapshots[candidate.groupId]
        if (current != null && !candidate.notificationTimestamp.isAfter(current.notificationTimestamp)) {
            throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_STALE,
                "Group config is not newer than the committed snapshot",
            )
        }
        val peers = candidate.membersByAgentId
            .filterKeys { it != localAgentId }
            .values
            .map { it.agentIp }
            .toSet()
        tunnelController.replaceGroupPeers(candidate.groupId, peers)
        val committed = candidate.copy(generation = (current?.generation ?: 0) + 1)
        snapshots[candidate.groupId] = committed
        committed
    }

    suspend fun resolve(groupId: String, agentId: String): GroupMemberInfo = mutex.withLock {
        val snapshot = snapshots[groupId] ?: throw AgentSdkException(
            ErrorCode.GROUP_NOT_ACTIVE,
            "Group $groupId has no committed configuration",
        )
        snapshot.membersByAgentId[agentId] ?: throw AgentSdkException(
            ErrorCode.TARGET_NOT_IN_GROUP,
            "Target $agentId is not in group $groupId",
        )
    }

    suspend fun snapshot(groupId: String): GroupConfigSnapshot? = mutex.withLock {
        snapshots[groupId]
    }

    suspend fun close() = mutex.withLock {
        snapshots.keys.toList().forEach { tunnelController.replaceGroupPeers(it, emptySet()) }
        snapshots.clear()
    }

    private fun parseServiceEndpoint(
        value: String,
        agentIp: String,
        field: String,
    ): Pair<String, Int> {
        val parsed = try {
            URI(value)
        } catch (error: Exception) {
            throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_INVALID,
                "$field must be an absolute HTTP/HTTPS URL",
                field,
                cause = error,
            )
        }
        if (
            parsed.scheme !in setOf("http", "https") ||
            parsed.host == null ||
            parsed.userInfo != null ||
            parsed.path.isNullOrBlank() ||
            parsed.fragment != null
        ) {
            throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_INVALID,
                "$field must be an absolute HTTP/HTTPS URL without credentials or fragment",
                field,
            )
        }
        val port = when {
            parsed.port != -1 -> parsed.port
            parsed.scheme == "https" -> 443
            else -> 80
        }
        if (port !in 1..65535) {
            throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_INVALID,
                "$field port must be in 1..65535",
                field,
            )
        }
        val deliveryEndpoint = URI(
            parsed.scheme,
            null,
            agentIp,
            port,
            parsed.path,
            parsed.query,
            null,
        ).toASCIIString()
        return deliveryEndpoint to port
    }

    private fun normalizeLiteralIp(value: String, field: String): String {
        val ipv4Candidate = IPV4_LITERAL.matches(value)
        val ipv6Candidate = value.contains(':') &&
            value.all { it.isDigit() || it in ":abcdefABCDEF" }
        if (!ipv4Candidate && !ipv6Candidate) {
            throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_INVALID,
                "$field must be a literal IP address",
                field,
            )
        }
        return try {
            val address = InetAddress.getByName(value)
            if ((ipv4Candidate && address.address.size != 4) ||
                (ipv6Candidate && address.address.size != 16)
            ) {
                throw IllegalArgumentException("address family mismatch")
            }
            requireNotNull(address.hostAddress).substringBefore('%')
        } catch (error: Exception) {
            throw AgentSdkException(
                ErrorCode.GROUP_CONFIG_INVALID,
                "$field is not a valid IP address",
                field,
                cause = error,
            )
        }
    }

    private companion object {
        val IPV4_LITERAL = Regex("(?:[0-9]{1,3}\\.){3}[0-9]{1,3}")
        val SEMANTIC_VERSION = Regex(
            "(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)" +
                "(?:-[0-9A-Za-z.-]+)?(?:\\+[0-9A-Za-z.-]+)?"
        )
    }
}
