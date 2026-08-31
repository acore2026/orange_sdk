package com.rayneo.agent.sdk.state

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.model.AgentLifecycleState
import com.rayneo.agent.sdk.model.AgentProfile
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.io.File
import java.security.MessageDigest
import java.util.UUID

internal data class RestoredAgentState(
    val state: AgentLifecycleState,
    val profile: AgentProfile?,
    val identityApplication: IdentityApplicationContext? = null,
    val agentCard: AgentCardContext? = null,
)

internal data class IdentityApplicationContext(
    val owner: String,
    val name: String,
    val description: String,
    val metadata: JsonObject,
)

internal data class AgentCardContext(
    val priority: Int,
    val vcList: List<JsonObject>,
)

internal interface AgentStateStore {
    fun load(runtimeHost: String, runtimePort: Int, agentTunIp: String): RestoredAgentState
    fun save(
        runtimeHost: String,
        runtimePort: Int,
        agentTunIp: String,
        state: AgentLifecycleState,
        profile: AgentProfile,
        identityApplication: IdentityApplicationContext,
        agentCard: AgentCardContext? = null,
    )
    fun clear(runtimeHost: String, runtimePort: Int)
}

internal class InMemoryAgentStateStore : AgentStateStore {
    private val states = mutableMapOf<String, RestoredAgentState>()
    private fun key(host: String, port: Int) = "${host.lowercase()}:$port"

    override fun load(
        runtimeHost: String,
        runtimePort: Int,
        agentTunIp: String,
    ): RestoredAgentState = states[key(runtimeHost, runtimePort)]
        ?: RestoredAgentState(AgentLifecycleState.NO_IDENTITY, null)

    override fun save(
        runtimeHost: String,
        runtimePort: Int,
        agentTunIp: String,
        state: AgentLifecycleState,
        profile: AgentProfile,
        identityApplication: IdentityApplicationContext,
        agentCard: AgentCardContext?,
    ) {
        states[key(runtimeHost, runtimePort)] = RestoredAgentState(
            state,
            profile,
            identityApplication,
            agentCard,
        )
    }

    override fun clear(runtimeHost: String, runtimePort: Int) {
        states.remove(key(runtimeHost, runtimePort))
    }
}

internal class FileAgentStateStore(private val directory: File) : AgentStateStore {
    private val json = Json { ignoreUnknownKeys = false }

    override fun load(
        runtimeHost: String,
        runtimePort: Int,
        agentTunIp: String,
    ): RestoredAgentState {
        val file = stateFile(runtimeHost, runtimePort)
        if (!file.exists()) return RestoredAgentState(AgentLifecycleState.NO_IDENTITY, null)
        try {
            val root = json.parseToJsonElement(file.readText()).jsonObject
            require(root["schema_version"]?.jsonPrimitive?.intOrNull == 2)
            val runtime = root.getValue("runtime").jsonObject
            require(runtime.getValue("host").jsonPrimitive.content == runtimeHost)
            require(runtime.getValue("port").jsonPrimitive.intOrNull == runtimePort)
            require(root.getValue("agent_tun_ip").jsonPrimitive.content == agentTunIp)
            val state = AgentLifecycleState.valueOf(
                root.getValue("state").jsonPrimitive.content
            )
            require(state != AgentLifecycleState.NO_IDENTITY)
            val storedProfile = root.getValue("profile").jsonObject
            val agentId = storedProfile.getValue("agent_id").jsonPrimitive.contentOrNull
                ?.takeIf(String::isNotBlank) ?: error("invalid profile.agent_id")
            val agentName = storedProfile.getValue("agent_name").jsonPrimitive.contentOrNull
                ?.takeIf(String::isNotBlank) ?: error("invalid profile.agent_name")
            val identityVc = storedProfile.getValue("identity_vc") as? JsonObject
                ?: error("invalid profile.identity_vc")
            val identityRaw = root.getValue("identity_application").jsonObject
            val identityApplication = IdentityApplicationContext(
                owner = identityRaw.getValue("owner").jsonPrimitive.content,
                name = identityRaw.getValue("name").jsonPrimitive.content,
                description = identityRaw.getValue("description").jsonPrimitive.content,
                metadata = identityRaw.getValue("metadata").jsonObject,
            )
            val agentCard = (root["agent_card"] as? JsonObject)?.let { card ->
                AgentCardContext(
                    priority = card.getValue("priority").jsonPrimitive.intOrNull
                        ?: error("invalid agent_card.priority"),
                    vcList = (card.getValue("vc_list") as? JsonArray)
                        ?.map { it as? JsonObject ?: error("invalid agent_card.vc_list") }
                        ?.takeIf(List<JsonObject>::isNotEmpty)
                        ?: error("invalid agent_card.vc_list"),
                )
            }
            require(state != AgentLifecycleState.CARD_PUBLISHED || agentCard != null)
            return RestoredAgentState(
                state,
                AgentProfile(agentId, agentName, identityVc),
                identityApplication,
                agentCard,
            )
        } catch (error: Exception) {
            throw AgentSdkException(
                ErrorCode.AGENT_STATE_INVALID,
                "Cannot restore Agent state from ${file.absolutePath}: ${error.message}",
                cause = error,
            )
        }
    }

    override fun save(
        runtimeHost: String,
        runtimePort: Int,
        agentTunIp: String,
        state: AgentLifecycleState,
        profile: AgentProfile,
        identityApplication: IdentityApplicationContext,
        agentCard: AgentCardContext?,
    ) {
        if (state == AgentLifecycleState.NO_IDENTITY) {
            throw AgentSdkException(
                ErrorCode.AGENT_STATE_INVALID,
                "NO_IDENTITY cannot be saved with an Agent profile",
            )
        }
        val file = stateFile(runtimeHost, runtimePort)
        val temporary = File(directory, ".${file.name}.${UUID.randomUUID()}.tmp")
        val document = buildJsonObject {
            put("schema_version", 2)
            put("runtime", buildJsonObject {
                put("host", runtimeHost)
                put("port", runtimePort)
            })
            put("agent_tun_ip", agentTunIp)
            put("state", state.name)
            put("profile", buildJsonObject {
                put("agent_id", profile.agentId)
                put("agent_name", profile.agentName)
                put("identity_vc", profile.identityVc)
            })
            put("identity_application", buildJsonObject {
                put("owner", identityApplication.owner)
                put("name", identityApplication.name)
                put("description", identityApplication.description)
                put("metadata", identityApplication.metadata)
            })
            agentCard?.let { card ->
                put("agent_card", buildJsonObject {
                    put("priority", card.priority)
                    put("vc_list", JsonArray(card.vcList))
                })
            }
        }
        try {
            check(directory.mkdirs() || directory.isDirectory)
            temporary.writeText(document.toString())
            temporary.setReadable(false, false)
            temporary.setWritable(false, false)
            temporary.setReadable(true, true)
            temporary.setWritable(true, true)
            check(temporary.renameTo(file)) { "atomic rename failed" }
        } catch (error: Exception) {
            temporary.delete()
            throw AgentSdkException(
                ErrorCode.AGENT_STATE_PERSISTENCE_FAILED,
                "Cannot persist Agent state to ${file.absolutePath}: ${error.message}",
                cause = error,
            )
        }
    }

    override fun clear(runtimeHost: String, runtimePort: Int) {
        val file = stateFile(runtimeHost, runtimePort)
        if (file.exists() && !file.delete()) {
            throw AgentSdkException(
                ErrorCode.AGENT_STATE_PERSISTENCE_FAILED,
                "Cannot clear Agent state file ${file.absolutePath}",
            )
        }
    }

    private fun stateFile(runtimeHost: String, runtimePort: Int): File {
        val endpoint = "${runtimeHost.lowercase()}:$runtimePort"
        val digest = MessageDigest.getInstance("SHA-256")
            .digest(endpoint.toByteArray())
            .joinToString("") { "%02x".format(it) }
        return File(directory, "$digest.json")
    }
}
