package com.rayneo.agent.sdk.agw

import com.rayneo.agent.sdk.model.GroupConfigSnapshot
import com.rayneo.agent.sdk.model.GroupMemberInfo
import com.rayneo.agent.sdk.transport.TunnelConfiguration
import com.rayneo.agent.sdk.transport.TunnelController
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test
import java.time.Instant
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicLong

class AGWManagerTest {
    @Test fun `public SDK exposes AGW names without legacy L5 names`() {
        val methods = com.rayneo.agent.sdk.AgentSdk::class.java.methods.map { it.name }.toSet()
        assertTrue("registerAGWTrack" in methods)
        assertTrue("subscribeAGWTrack" in methods)
        assertTrue("publishAGWMessage" in methods)
        assertTrue("getAGWCatalog" in methods)
        assertTrue(methods.none { it.contains("L5") || it.startsWith("register_l5") })
    }

    private class Routes : TunnelController {
        override val tunFd = -1
        override val clientIdentityDirectory = ""
        val peers = ConcurrentHashMap<String, Set<String>>()
        override suspend fun establish(configuration: TunnelConfiguration) {}
        override suspend fun replaceGroupPeers(groupId: String, peerIps: Set<String>) {
            if (peerIps.isEmpty()) peers.remove(groupId) else peers[groupId] = peerIps
        }
        override suspend fun close() { peers.clear() }
        override fun currentAllowedPeerIps(): Set<String> = peers.values.flatten().toSet()
        override fun setTunFdSwapper(swapper: suspend (Int) -> Unit) {}
    }

    private class Bridge : AGWBridge {
        val sequence = AtomicLong()
        val opened = ConcurrentHashMap<Long, String>()
        val events = ConcurrentHashMap<Long, Channel<JsonObject>>()
        val requests = AtomicLong()
        override suspend fun open(configuration: JsonObject): Long {
            val id = sequence.incrementAndGet()
            opened[id] = configuration.getValue("role").jsonPrimitive.content
            events[id] = Channel(Channel.UNLIMITED)
            return id
        }
        override suspend fun call(handle: Long, command: JsonObject): JsonObject {
            return when (command.getValue("operation").jsonPrimitive.content) {
                "poll" -> withTimeoutOrNull(30) { events.getValue(handle).receive() } ?: buildJsonObject { put("type", "idle") }
                "request" -> {
                    val id = requests.incrementAndGet()
                    events.getValue(handle).send(buildJsonObject { put("type", "accepted"); put("request_id", id) })
                    if (opened[handle] == "subscriber" && command["name"]?.jsonPrimitive?.content == "group_catalog") {
                        events.getValue(handle).send(buildJsonObject {
                            put("type", "object"); put("request_id", id)
                            put("payload", buildJsonObject {
                                put("group_id", "g"); put("group_version", 1); put("publisher_agent_id", "B")
                                put("directory_revision", 1); put("tracks", buildJsonArray {})
                            })
                        })
                    }
                    buildJsonObject { put("request_id", id) }
                }
                else -> buildJsonObject { put("submitted", true) }
            }
        }
        override fun close(handle: Long) { opened.remove(handle) }
    }

    private fun snapshot(version: ULong = 1u) = GroupConfigSnapshot("g", "1.0.0", Instant.now(),
        listOf("A", "B").associateWith { GroupMemberInfo(it, it, emptyList(), "127.0.0.1", 1, 0, "") },
        1, version)

    @Test fun `namespace matches Python SHA256 Base32 and full uint64`() {
        assertEquals("kwnovuecmtkxsxjzbfyyzxifvpkjk4xij7svleho6mniriep376q", AGWManager.identifierKey("A"))
        assertEquals("18446744073709551615", AGWManager.namespace("g", ULong.MAX_VALUE, "A")[5])
    }

    @Test fun `two roles and catalog are independent of application listener`() = runBlocking {
        val bridge = Bridge(); val routes = Routes()
        val manager = AGWManager(AGWConfig("127.0.0.1"), "A", "127.0.0.1", routes, bridge)
        try {
            val config = snapshot()
            manager.configure(config)
            withTimeout(3_000) { while (manager.catalog("g", "B") == null) delay(10) }
            withTimeout(3_000) {
                while (manager.state("g").none {
                    it["direction"]?.jsonPrimitive?.content == "publisher" &&
                        it["track"]?.jsonPrimitive?.content == "group_catalog" &&
                        it["state"]?.jsonPrimitive?.content == "READY"
                }) delay(10)
            }
            manager.registerTrack("g", "status")
            manager.publish("g", "status", buildJsonObject { put("state", "ready") })
            assertEquals(setOf("publisher", "subscriber"), bridge.opened.values.toSet())
            assertEquals(2, bridge.opened.size)
            val count = bridge.requests.get()
            manager.configure(config)
            manager.retry("g")
            delay(50)
            assertEquals(count, bridge.requests.get())
            assertEquals(setOf("127.0.0.1"), routes.peers.values.flatten().toSet())
            manager.invalidate("g")
            withTimeout(3_000) { while (bridge.opened.isNotEmpty() || routes.peers.isNotEmpty()) delay(10) }
        } finally { manager.close() }
    }
}
