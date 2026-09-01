package com.rayneo.agent.example

enum class TestRole(
    val displayName: String,
    val defaultRuntimePort: Int,
    val defaultMasquePort: Int,
    val defaultMessage: String,
) {
    A("角色 A · 发起方", 8088, 8443, "hello Agent B from Android A"),
    B("角色 B · 能力提供方", 8089, 8444, "hello Agent A from Android B"),
}

data class TestConfig(
    val role: TestRole,
    val serverIp: String,
    val runtimePort: Int,
    val masquePort: Int,
    val masquePath: String,
    val localTcpPort: Int,
    val localUdpPort: Int,
    val masqueToken: String?,
    val owner: String,
    val agentName: String,
    val capability: String,
    val dnn: String,
    val groupName: String,
    val message: String,
) {
    val masqueServerUrl: String
        get() = "https://$serverIp:$masquePort${normalizedMasquePath()}"

    fun validate(): List<String> = buildList {
        if (serverIp.isBlank()) add("服务器 IP 不能为空")
        listOf(
            "Runtime HTTP 端口" to runtimePort,
            "MASQUE QUIC 端口" to masquePort,
            "本地 TCP 端口" to localTcpPort,
            "本地 UDP 端口" to localUdpPort,
        ).forEach { (name, port) ->
            if (port !in 1..65535) add("$name 必须在 1..65535")
        }
        if (masquePath.isBlank()) add("MASQUE 路径不能为空")
        if (owner.isBlank()) add("Owner 不能为空")
        if (agentName.isBlank()) add("Agent 名称不能为空")
        if (capability.isBlank()) add("发现能力不能为空")
        if (role == TestRole.A && dnn.isBlank()) add("角色 A 的 DNN 不能为空")
        if (role == TestRole.A && groupName.isBlank()) add("角色 A 的群组名不能为空")
        if (message.isBlank()) add("手动消息预填内容不能为空")
    }

    private fun normalizedMasquePath(): String =
        masquePath.trim().let { if (it.startsWith('/')) it else "/$it" }
}

object RayNeoX3ProDeployment {
    const val SERVER_IP = "101.245.78.174"
    const val RUNTIME_PORT = 8088
    const val MASQUE_PORT = 8443
    const val MASQUE_PATH = "/.well-known/masque/ip"

    fun agentAConfig(masqueToken: String? = null): TestConfig = TestConfig(
        role = TestRole.A,
        serverIp = SERVER_IP,
        runtimePort = RUNTIME_PORT,
        masquePort = MASQUE_PORT,
        masquePath = MASQUE_PATH,
        localTcpPort = 4001,
        localUdpPort = 28443,
        masqueToken = masqueToken?.takeIf(String::isNotBlank),
        owner = "rayneo-x3-pro-owner-a",
        agentName = "RayNeo-X3-Pro-A",
        capability = "text",
        dnn = "internet",
        groupName = "rayneo-x3-pro-ab-group",
        message = "hello Agent B from RayNeo X3 Pro",
    )
}
