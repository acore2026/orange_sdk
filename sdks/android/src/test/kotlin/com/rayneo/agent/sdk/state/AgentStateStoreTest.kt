package com.rayneo.agent.sdk.state

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.model.AgentLifecycleState
import com.rayneo.agent.sdk.model.AgentProfile
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Test
import java.nio.file.Files

class AgentStateStoreTest {
    @Test
    fun `file store restores profile and rejects a changed PDU address`() {
        val directory = Files.createTempDirectory("agent-state-store-").toFile()
        val store = FileAgentStateStore(directory)
        val profile = AgentProfile(
            "did:example:a",
            "Agent A",
            buildJsonObject { put("id", "vc0-a") },
        )
        val identityApplication = IdentityApplicationContext(
            owner = "Alice",
            name = "Agent A",
            description = "test",
            metadata = buildJsonObject {
                put("region", "CN")
                put("os", "Android")
                put("version", "0.14.0")
            },
        )

        store.save(
            "192.168.3.10",
            8088,
            "10.60.0.2",
            AgentLifecycleState.IDENTITY_READY,
            profile,
            identityApplication,
        )
        store.save(
            "192.168.3.10",
            8088,
            "10.60.0.2",
            AgentLifecycleState.CARD_PUBLISHED,
            profile,
            identityApplication,
            AgentCardContext(1, listOf(buildJsonObject { put("id", "vc-a") })),
        )
        val restored = store.load("192.168.3.10", 8088, "10.60.0.2")

        assertEquals(AgentLifecycleState.CARD_PUBLISHED, restored.state)
        assertEquals(profile, restored.profile)
        assertNotNull(directory.listFiles()?.singleOrNull())

        val error = runCatching {
            store.load("192.168.3.10", 8088, "10.60.0.99")
        }.exceptionOrNull() as AgentSdkException
        assertEquals(ErrorCode.AGENT_STATE_INVALID, error.code)

        store.clear("192.168.3.10", 8088)
        assertEquals(
            AgentLifecycleState.NO_IDENTITY,
            store.load("192.168.3.10", 8088, "10.60.0.2").state,
        )
        assertNotEquals(
            AgentLifecycleState.CARD_PUBLISHED,
            store.load("192.168.3.10", 8089, "10.60.0.2").state,
        )
    }
}
