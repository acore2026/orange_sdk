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
        assertTrue(TestRole.A.defaultMessage.contains("Agent B"))
        assertTrue(TestRole.B.defaultMessage.contains("Agent A"))
    }

    @Test
    fun rayNeoX3ProDeploymentIsFixedAsAgentA() {
        val config = RayNeoX3ProDeployment.agentAConfig()

        assertEquals(TestRole.A, config.role)
        assertEquals("101.245.78.174", config.serverIp)
        assertEquals(8088, config.runtimePort)
        assertEquals(8443, config.masquePort)
        assertEquals("172.30.0.10", config.computeControlIp)
        assertEquals(28500, config.computeControlPort)
        assertEquals(
            "https://101.245.78.174:8443/.well-known/masque/ip",
            config.masqueServerUrl,
        )
        assertTrue(config.validate().isEmpty())
    }

    @Test
    fun masqueUrlIsComposedFromUserSuppliedFields() {
        val config = validConfig(masquePath = "custom/connect-ip")

        assertEquals("https://runtime.test:8443/custom/connect-ip", config.masqueServerUrl)
    }

    @Test
    fun roleARequiresGroupFieldsButBothRolesRequireAMessageDraft() {
        val invalidA = validConfig(dnn = "", groupName = "", message = "")
        val validB = invalidA.copy(role = TestRole.B, message = TestRole.B.defaultMessage)

        assertTrue(invalidA.validate().any { it.contains("DNN") })
        assertTrue(invalidA.validate().any { it.contains("群组名") })
        assertTrue(invalidA.validate().any { it.contains("手动消息") })
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
