package com.rayneo.agent.sdk.l5

data class L5Config(
    val relayIpv4: String,
    val relayPort: Int = 4433,
    val recoveryTimeoutMillis: Long = 30_000,
    val localTimeoutMillis: Long = 5_000,
) {
    init {
        val parts = relayIpv4.split('.')
        require(parts.size == 4 && parts.all { part -> part.toIntOrNull()?.let { it in 0..255 && part == it.toString() } == true }) { "Explicit Relay IPv4 required" }
        require(relayPort in 1..65535)
        require(localTimeoutMillis > 0 && recoveryTimeoutMillis >= localTimeoutMillis)
    }
}
