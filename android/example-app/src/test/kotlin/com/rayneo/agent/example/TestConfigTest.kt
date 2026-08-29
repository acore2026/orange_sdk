package com.rayneo.agent.example

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class TestConfigTest {
    @Test
    fun roleDefaultsMatchTheLinuxTwoInstanceDeployment() {
        assertEquals(8088, TestRole.A.defaultRuntimePort)
        assertEquals(8443, TestRole.A.defaultMasquePort)
        assertEquals(8089, TestRole.B.defaultRuntimePort)
        assertEquals(8444, TestRole.B.defaultMasquePort)
    }

    @Test
    fun masqueUrlIsComposedFromUserSuppliedFields() {
        val config = validConfig(masquePath = "custom/connect-ip")

        assertEquals("https://runtime.test:8443/custom/connect-ip", config.masqueServerUrl)
    }

    @Test
    fun roleARequiresTaskFieldsButRoleBDoesNot() {
        val invalidA = validConfig(dnn = "", groupName = "", message = "")
        val validB = invalidA.copy(role = TestRole.B)

        assertTrue(invalidA.validate().any { it.contains("DNN") })
        assertTrue(invalidA.validate().any { it.contains("群组名") })
        assertTrue(invalidA.validate().any { it.contains("测试消息") })
        assertTrue(validB.validate().isEmpty())
    }

    @Test
    fun invalidPortIsRejectedBeforeVpnStartup() {
        val config = validConfig(runtimePort = 0)

        assertTrue(config.validate().any { it.contains("Runtime HTTP 端口") })
    }

    private fun validConfig(
        role: TestRole = TestRole.A,
        runtimePort: Int = 8088,
        masquePath: String = "/.well-known/masque/ip",
        dnn: String = "internet",
        groupName: String = "android-test",
        message: String = "hello",
    ) = TestConfig(
        role = role,
        serverIp = "runtime.test",
        runtimePort = runtimePort,
        masquePort = 8443,
        masquePath = masquePath,
        localVlanIp = "device.test",
        localTcpPort = 4001,
        localUdpPort = 28443,
        masqueToken = null,
        owner = "test-owner",
        agentName = "Agent-${role.name}",
        capability = "text",
        dnn = dnn,
        groupName = groupName,
        message = message,
    )
}
