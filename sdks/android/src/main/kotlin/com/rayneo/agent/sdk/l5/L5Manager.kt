package com.rayneo.agent.sdk.l5

import android.util.Log
import com.rayneo.agent.sdk.model.GroupConfigSnapshot
import com.rayneo.agent.sdk.transport.TunnelController
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.*
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap

internal class L5RequestFailure(val retryInterval: Long, message: String) : Exception(message)

@OptIn(kotlinx.serialization.ExperimentalSerializationApi::class)
class L5Manager internal constructor(
    private val config: L5Config,
    private val agentId: String,
    private val sourceIp: String,
    private val tunnel: TunnelController,
    private val bridge: L5Bridge = NativeL5Bridge(),
) {
    private data class Key(val group: String, val role: String, val member: String, val name: String)
    private class Role(val handle: Long) {
        val requests = ConcurrentHashMap<Long, CompletableDeferred<Unit>>()
        val owners = ConcurrentHashMap<Long, Key>()
        val mutex = Mutex()
        var poll: Job? = null
    }
    private data class Request(val role: Role, val id: Long)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val groups = ConcurrentHashMap<String, GroupConfigSnapshot>()
    private val roles = ConcurrentHashMap<String, Role>()
    private val connections = ConcurrentHashMap<String, Deferred<Role>>()
    private val roleLock = Mutex()
    private val routeLock = Mutex()
    private val roleBlocked = ConcurrentHashMap.newKeySet<String>()
    private val blocked = ConcurrentHashMap.newKeySet<Key>()
    private val work = ConcurrentHashMap<Key, Deferred<Unit>>()
    private val requests = ConcurrentHashMap<Key, Request>()
    private val tracks = ConcurrentHashMap<String, ConcurrentHashMap<String, String>>()
    private val wanted = ConcurrentHashMap.newKeySet<Key>()
    private val catalogs = ConcurrentHashMap<Pair<String, String>, JsonObject>()
    private val revisions = ConcurrentHashMap<Pair<String, String>, ULong>()
    private val firstCatalog = ConcurrentHashMap<Key, CompletableDeferred<Unit>>()
    private val ownRevision = ConcurrentHashMap<String, ULong>()
    private val catalogLocks = ConcurrentHashMap<String, Mutex>()
    private val catalogChanges = ConcurrentHashMap<String, Long>()
    private val catalogJobs = ConcurrentHashMap<String, Job>()
    private val catalogSentAt = ConcurrentHashMap<String, Long>()
    private val statuses = ConcurrentHashMap<Key, String>()
    @Volatile private var closed = false
    @Volatile var listener: ((JsonObject) -> Unit)? = null
    private val routeKey = "\u0000acn-l5-relay"

    private fun now() = System.nanoTime() / 1_000_000
    private fun emit(type: String, key: Key? = null, reason: String? = null, value: JsonObject? = null) {
        val event = buildJsonObject {
            put("event", type)
            key?.let {
                put("group_id", it.group); put("direction", it.role); put("member_agent_id", it.member); put("track", it.name)
                groups[it.group]?.let { group -> put("group_version", JsonPrimitive(group.groupVersion)) }
                if (type == "message") put("publisher_agent_id", it.member)
                if (type == "unavailable") put("explicit_retry_required", it in blocked)
            }
            reason?.let { put("reason", it) }
            value?.let { put(if (type == "catalog") "catalog" else "payload", it) }
        }
        Log.i("AgentSdkL5", event.toString())
        scope.launch { runCatching { listener?.invoke(event) } }
    }

    fun configure(snapshot: GroupConfigSnapshot) {
        if (groups[snapshot.groupId]?.groupVersion == snapshot.groupVersion) return
        invalidate(snapshot.groupId, false)
        groups[snapshot.groupId] = snapshot
        wanted.removeIf { it.group == snapshot.groupId && it.member !in snapshot.membersByAgentId }
        startGroup(snapshot.groupId)
    }
    private fun startGroup(group: String) {
        start(Key(group, "publisher", agentId, "group_catalog"))
        groups[group]?.membersByAgentId?.keys?.filter { it != agentId }?.forEach { start(Key(group, "subscriber", it, "group_catalog")) }
        wanted.filter { it.group == group }.forEach { start(it) }
    }
    private fun valid(group: String, version: ULong) = !closed && groups[group]?.groupVersion == version
    private fun remaining(deadline: Long) = (deadline - now()).coerceAtLeast(1)

    @Synchronized private fun start(key: Key, explicit: Boolean = false): Deferred<Unit>? {
        val version = groups[key.group]?.groupVersion ?: return null
        if (closed) return null
        if (explicit) { blocked.remove(key); roleBlocked.remove(key.role) }
        if (key in blocked) return null
        work[key]?.takeIf { it.isActive }?.let { return it }
        statuses[key] = "PREPARING"
        val task = scope.async {
            val deadline = now() + config.recoveryTimeoutMillis
            try {
                withTimeout(config.recoveryTimeoutMillis) {
                    while (valid(key.group, version)) {
                        try {
                            prepare(key, version, deadline)
                            if (valid(key.group, version)) { statuses[key] = "READY"; emit("ready", key) }
                            return@withTimeout
                        } catch (error: L5RequestFailure) {
                            if (error.retryInterval == 0L) throw error
                            delay((error.retryInterval - 1).coerceAtLeast(100).coerceAtMost(remaining(deadline)))
                        }
                    }
                }
            } catch (error: TimeoutCancellationException) {
                failWork(key, "deadline exceeded; explicit retry required")
            } catch (error: CancellationException) { throw error
            } catch (error: Exception) {
                failWork(key, error.message)
            }
        }
        work[key] = task
        return task
    }

    private suspend fun failWork(key: Key, reason: String?) {
        blocked.add(key); statuses[key] = "UNAVAILABLE"
        if (key.role == "subscriber" && key.name == "group_catalog") catalogs.remove(key.group to key.member)
        withContext(NonCancellable) { requests.remove(key)?.let { cancel(it) } }
        emit("unavailable", key, reason)
    }

    private suspend fun role(name: String, deadline: Long): Role {
        roles[name]?.let { return it }
        val task = roleLock.withLock {
            roles[name]?.let { return it }
            check(name !in roleBlocked) { "role requires explicit retry" }
            connections[name]?.takeIf { it.isActive } ?: scope.async {
                try {
                    withTimeout(config.recoveryTimeoutMillis) {
                        routeLock.withLock { tunnel.replaceGroupPeers(routeKey, setOf(config.relayIpv4)) }
                        while (true) {
                            try {
                                val handle = bridge.open(buildJsonObject {
                                    put("host", config.relayIpv4); put("port", config.relayPort); put("source_ip", sourceIp)
                                    put("agent_id", agentId); put("role", name)
                                })
                                if (closed || groups.isEmpty()) { bridge.close(handle); throw CancellationException("no active L5 group") }
                                val connected = Role(handle)
                                roles[name] = connected
                                if (name == "publisher") ownRevision.clear()
                                connected.poll = scope.launch { poll(name, connected) }
                                return@withTimeout connected
                            } catch (error: CancellationException) { throw error
                            } catch (error: Exception) {
                                if (error is L5BindingException && error.code != "AGENT_NOT_REGISTERED") throw error
                                delay(500)
                            }
                        }
                        @Suppress("UNREACHABLE_CODE") error("unreachable")
                    }
                } catch (error: Exception) { roleBlocked.add(name); throw error }
            }.also { connections[name] = it }
        }
        return withTimeout(remaining(deadline)) { task.await() }
    }

    private suspend fun request(key: Key, version: ULong, deadline: Long): Request {
        val connection = role(key.role, deadline)
        requests[key]?.takeIf { it.role === connection }?.let { return it }
        val request = connection.mutex.withLock {
            requests[key]?.takeIf { it.role === connection }?.let { return@withLock it }
            val response = bridge.call(connection.handle, buildJsonObject {
                put("operation", "request"); put("namespace", JsonArray(namespace(key.group, version, key.member).map(::JsonPrimitive))); put("name", key.name)
            })
            val id = response.getValue("request_id").jsonPrimitive.long
            val pending = CompletableDeferred<Unit>()
            connection.requests[id] = pending
            connection.owners[id] = key
            val result = Request(connection, id)
            requests[key] = result
            result
        }
        try {
            connection.requests[request.id]?.let { withTimeout(minOf(config.localTimeoutMillis, remaining(deadline))) { it.await() } }
            check(valid(key.group, version)) { "group version changed" }
            return request
        } catch (error: Exception) {
            requests.remove(key, request)
            withContext(NonCancellable) { cancel(request) }
            throw error
        }
    }

    private suspend fun prepare(key: Key, version: ULong, deadline: Long) {
        if (key.name == "group_catalog" && key.role == "publisher") {
            tracks[key.group]?.keys?.forEach { name ->
                val business = Key(key.group, "publisher", agentId, name)
                request(business, version, deadline); statuses[business] = "READY"
            }
            publishCatalog(key.group, version, deadline)
        } else if (key.name == "group_catalog") {
            revisions.remove(key.group to key.member)
            val first = CompletableDeferred<Unit>()
            firstCatalog[key] = first
            try {
                request(key, version, deadline)
                withTimeout(minOf(config.localTimeoutMillis, remaining(deadline))) { first.await() }
                statuses[key] = "READY"
                catalogs[key.group to key.member]?.let { emit("catalog", key, value = it) }
            } finally { firstCatalog.remove(key, first) }
        } else request(key, version, deadline)
    }

    private suspend fun publishCatalog(group: String, version: ULong, deadline: Long) = catalogLocks.getOrPut(group) { Mutex() }.withLock {
        val key = Key(group, "publisher", agentId, "group_catalog")
        val request = request(key, version, deadline)
        delay(((catalogSentAt[group] ?: 0) + 500 - now()).coerceAtLeast(0))
        if (!valid(group, version)) return@withLock
        val revision = (ownRevision[group] ?: 0u) + 1u
        ownRevision[group] = revision; catalogSentAt[group] = now()
        val value = buildJsonObject {
            put("group_id", group); put("group_version", JsonPrimitive(version)); put("publisher_agent_id", agentId)
            put("directory_revision", JsonPrimitive(revision))
            put("tracks", JsonArray(tracks[group].orEmpty().toSortedMap().map { (name, content) ->
                buildJsonObject { put("name", name); put("content_type", content) }
            }))
        }
        send(request, value)
    }

    private fun queueCatalog(group: String) {
        catalogChanges.merge(group, 1L, Long::plus)
        if (catalogJobs[group]?.isActive == true) return
        val version = groups[group]?.groupVersion ?: return
        catalogJobs[group] = scope.launch {
            try {
                val deadline = now() + config.recoveryTimeoutMillis
                do {
                    val change = catalogChanges[group]
                    publishCatalog(group, version, deadline)
                } while (valid(group, version) && change != catalogChanges[group])
            } catch (error: Exception) { emit("unavailable", Key(group, "publisher", agentId, "group_catalog"), error.message) }
        }
    }

    private suspend fun poll(name: String, connection: Role) {
        try {
            while (!closed && roles[name] === connection) {
                val event = connection.mutex.withLock { bridge.call(connection.handle, buildJsonObject { put("operation", "poll") }) }
                val id = event["request_id"]?.jsonPrimitive?.longOrNull
                when (event["type"]?.jsonPrimitive?.content) {
                    "accepted" -> connection.requests[id]?.complete(Unit)
                    "failed" -> {
                        val key = connection.owners.remove(id) ?: continue
                        val failure = L5RequestFailure(event["retry_interval"]?.jsonPrimitive?.longOrNull ?: 0, event["reason"]?.jsonPrimitive?.content ?: "Track ended")
                        val pending = connection.requests.remove(id)
                        if (pending?.isCompleted == false) pending.completeExceptionally(failure)
                        else { requests.remove(key); statuses[key] = "UNAVAILABLE"; if (key.name == "group_catalog") catalogs.remove(key.group to key.member); emit("unavailable", key, failure.message); start(key) }
                    }
                    "object" -> {
                        val key = connection.owners[id] ?: continue
                        if (key in blocked) continue
                        val value = event.getValue("payload").jsonObject
                        if (key.name == "group_catalog") receiveCatalog(key, value) else emit("message", key, value = value)
                    }
                    "closed" -> {
                        val code = event["code"]?.jsonPrimitive?.longOrNull ?: 0
                        if (code == 0xAC10L) roleBlocked.add(name)
                        break
                    }
                }
            }
        } catch (error: CancellationException) { throw error
        } catch (error: Exception) { emit("role_error", reason = error.message)
        } finally {
            if (roles.remove(name, connection)) {
                bridge.close(connection.handle)
                connection.requests.values.forEach { if (!it.isCompleted) it.completeExceptionally(L5RequestFailure(501, "role disconnected")) }
                requests.entries.removeIf { it.value.role === connection }
                statuses.keys.filter { it.role == name }.forEach { statuses[it] = "UNAVAILABLE" }
                if (!closed && name !in roleBlocked) groups.keys.forEach { group ->
                    if (name == "publisher") start(Key(group, name, agentId, "group_catalog"))
                    else { groups[group]?.membersByAgentId?.keys?.filter { it != agentId }?.forEach { start(Key(group, name, it, "group_catalog")) }; wanted.filter { it.group == group }.forEach { start(it) } }
                }
            }
        }
    }

    private fun receiveCatalog(key: Key, value: JsonObject) {
        val group = groups[key.group] ?: return
        try {
            require(value["group_id"]?.jsonPrimitive?.content == key.group && value["group_version"]?.jsonPrimitive?.content?.toULongOrNull() == group.groupVersion && value["publisher_agent_id"]?.jsonPrimitive?.content == key.member)
            val revision = value.getValue("directory_revision").jsonPrimitive.content.toULong()
            require(revision > 0u)
            val entries = value.getValue("tracks").jsonArray
            require(entries.size <= 64)
            val names = entries.map { item ->
                val name = item.jsonObject.getValue("name").jsonPrimitive.content
                require(name.isNotEmpty() && name != "group_catalog" && name.toByteArray().size <= 128 && item.jsonObject.getValue("content_type").jsonPrimitive.content.isNotEmpty())
                name
            }
            require(names.toSet().size == names.size)
            val directoryScope = key.group to key.member
            revisions[directoryScope]?.let { old -> require(revision >= old && (revision != old || catalogs[directoryScope] == value)) }
            revisions[directoryScope] = revision; catalogs[directoryScope] = value
            firstCatalog[key]?.complete(Unit)
            wanted.filter { it.group == key.group && it.member == key.member && it.name !in names }.forEach { removed ->
                statuses[removed] = "UNAVAILABLE"; requests.remove(removed)?.let { scope.launch { cancel(it) } }
            }
            if (statuses[key] == "READY") emit("catalog", key, value = value)
        } catch (error: Exception) {
            catalogs.remove(key.group to key.member); statuses[key] = "UNAVAILABLE"
            requests.remove(key)?.let { request -> scope.launch { cancel(request) } }
            emit("unavailable", key, "Invalid catalog: ${error.message}")
        }
    }

    suspend fun registerTrack(group: String, name: String, contentType: String = "application/json") {
        requireGroup(group); require(name.isNotEmpty() && name != "group_catalog" && name.toByteArray().size <= 128 && contentType.isNotEmpty())
        val entries = tracks.getOrPut(group) { ConcurrentHashMap() }
        require(entries.containsKey(name) || entries.size < 64)
        entries[name] = contentType
        val key = Key(group, "publisher", agentId, name)
        start(key)?.await(); check(statuses[key] == "READY") { "L5 publication unavailable" }
        queueCatalog(group)
    }
    suspend fun unregisterTrack(group: String, name: String) {
        requireGroup(group); tracks[group]?.remove(name)
        requests.remove(Key(group, "publisher", agentId, name))?.let { cancel(it) }; queueCatalog(group)
    }
    suspend fun subscribe(group: String, member: String, name: String) {
        requireGroup(group); require(member != agentId && member in groups.getValue(group).membersByAgentId && name.isNotEmpty() && name != "group_catalog" && name.toByteArray().size <= 128)
        val key = Key(group, "subscriber", member, name); wanted.add(key)
        start(key)?.await(); check(statuses[key] == "READY") { "L5 subscription unavailable" }
    }
    suspend fun unsubscribe(group: String, member: String, name: String) {
        val key = Key(group, "subscriber", member, name); wanted.remove(key); work.remove(key)?.cancel()
        requests.remove(key)?.let { cancel(it) }; statuses.remove(key)
    }
    suspend fun publish(group: String, name: String, value: JsonObject, datagram: Boolean = false) {
        requireGroup(group); val key = Key(group, "publisher", agentId, name)
        require(name != "group_catalog" && tracks[group]?.containsKey(name) == true)
        check(statuses[key] == "READY") { "L5 publication unavailable" }
        send(requests.getValue(key), value, datagram)
    }
    private suspend fun send(request: Request, value: JsonObject, datagram: Boolean = false) {
        require(value.toString().toByteArray().size <= 65536)
        bridge.call(request.role.handle, buildJsonObject { put("operation", "send"); put("request_id", request.id); put("payload", value); put("datagram", datagram) })
    }
    private suspend fun cancel(request: Request) {
        request.role.owners.remove(request.id); request.role.requests.remove(request.id)
        runCatching { bridge.call(request.role.handle, buildJsonObject { put("operation", "cancel"); put("request_id", request.id) }) }
    }
    fun retry(group: String, direction: String? = null) {
        requireGroup(group); require(direction == null || direction in setOf("publisher", "subscriber"))
        statuses.keys.filter { it.group == group && it.member in groups.getValue(group).membersByAgentId && statuses[it] != "READY" && (direction == null || it.role == direction) }.forEach { start(it, true) }
    }
    fun catalog(group: String, member: String): JsonObject? { requireGroup(group); return if (statuses[Key(group, "subscriber", member, "group_catalog")] == "READY") catalogs[group to member] else null }
    fun state(group: String): List<JsonObject> { requireGroup(group); return statuses.filterKeys { it.group == group }.map { (key, value) -> buildJsonObject { put("direction", key.role); put("member_agent_id", key.member); put("track", key.name); put("state", value) } } }
    private fun requireGroup(group: String) { check(!closed && groups.containsKey(group)) { "No active ACN group" } }

    fun invalidate(group: String, release: Boolean = true) {
        groups.remove(group)
        work.keys.filter { it.group == group }.forEach { work.remove(it)?.cancel() }
        catalogJobs.remove(group)?.cancel()
        requests.keys.filter { it.group == group }.forEach { key -> requests.remove(key)?.let { scope.launch { cancel(it) } } }
        catalogs.keys.filter { it.first == group }.forEach { catalogs.remove(it); revisions.remove(it) }
        statuses.keys.filter { it.group == group }.forEach { statuses[it] = "UNAVAILABLE" }
        if (release) { tracks.remove(group); wanted.removeIf { it.group == group } }
        if (release && groups.isEmpty()) scope.launch { releaseRoles() }
    }
    private suspend fun releaseRoles() = routeLock.withLock {
        if (groups.isNotEmpty() && !closed) return@withLock
        connections.values.forEach { it.cancel() }
        connections.values.forEach { runCatching { it.await() } }
        roles.values.forEach { it.poll?.cancel(); bridge.close(it.handle) }; roles.clear()
        tunnel.replaceGroupPeers(routeKey, emptySet())
    }
    suspend fun close() {
        closed = true
        groups.keys.toList().forEach { invalidate(it, false) }
        releaseRoles(); scope.cancel()
    }

    companion object {
        private const val alphabet = "abcdefghijklmnopqrstuvwxyz234567"
        fun identifierKey(value: String): String {
            require(value.isNotEmpty())
            val hash = MessageDigest.getInstance("SHA-256").digest(value.toByteArray(Charsets.UTF_8))
            var accumulator = 0; var bits = 0; val result = StringBuilder()
            hash.forEach { byte -> accumulator = (accumulator shl 8) or (byte.toInt() and 255); bits += 8
                while (bits >= 5) { bits -= 5; result.append(alphabet[(accumulator ushr bits) and 31]) }
            }
            if (bits > 0) result.append(alphabet[(accumulator shl (5 - bits)) and 31])
            return result.toString()
        }
        fun namespace(group: String, version: ULong, agent: String): List<String> {
            require(version > 0u)
            return listOf("acn", "v1", "group", identifierKey(group), "version", version.toString(), "agent", identifierKey(agent))
        }
    }
}
