package com.rayneo.agent.example

import com.rayneo.agent.sdk.model.GroupConfigSnapshot
import com.rayneo.agent.sdk.model.GroupMemberInfo
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import java.time.Instant

class ManualMessagingTest {
    @Test
    fun eitherRoleSelectsTheOtherGroupMemberAsManualMessageTarget() {
        val snapshot = snapshot(member("agent-a", "Agent-A"), member("agent-b", "Agent-B"))

        val fromA = selectManualMessageSession(snapshot, "agent-a")
        val fromB = selectManualMessageSession(snapshot, "agent-b")

        assertEquals("agent-b", fromA.targetAgentId)
        assertEquals("Agent-B", fromA.targetAgentName)
        assertEquals("agent-a", fromB.targetAgentId)
        assertEquals("Agent-A", fromB.targetAgentName)
        assertEquals("group-ab", fromA.groupId)
        assertEquals("group-ab", fromB.groupId)
    }

    @Test
    fun manualMessageModeRejectsAGroupWithoutExactlyOnePeer() {
        val noPeer = snapshot(member("agent-a", "Agent-A"))
        val twoPeers = snapshot(
            member("agent-a", "Agent-A"),
            member("agent-b", "Agent-B"),
            member("agent-c", "Agent-C"),
        )

        assertThrows(IllegalStateException::class.java) {
            selectManualMessageSession(noPeer, "agent-a")
        }
        assertThrows(IllegalStateException::class.java) {
            selectManualMessageSession(twoPeers, "agent-a")
        }
    }

    private fun snapshot(vararg members: GroupMemberInfo) = GroupConfigSnapshot(
        groupId = "group-ab",
        version = "1.0.0",
        notificationTimestamp = Instant.parse("2026-08-31T00:00:00Z"),
        membersByAgentId = members.associateBy { it.agentId },
        generation = 1,
    )

    private fun member(agentId: String, agentName: String) = GroupMemberInfo(
        agentId = agentId,
        agentName = agentName,
        capabilities = listOf("text"),
        agentIp = if (agentId == "agent-a") "10.60.0.2" else "10.60.0.3",
        tcpPort = 4001,
        udpPort = 0,
        didKey = "",
        serviceEndpoint = "http://10.60.0.2:4001/A2A/message",
    )
}
