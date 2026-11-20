package com.rayneo.agent.sdk.agw

import androidx.annotation.Keep
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.*

interface AGWBridge {
    suspend fun open(configuration: JsonObject): Long
    suspend fun call(handle: Long, command: JsonObject): JsonObject
    fun close(handle: Long)
}

class AGWBindingException(val code: String, message: String) : IllegalStateException(message)

@Keep
class NativeAGWBridge : AGWBridge {
    companion object {
        private val loading = runCatching { System.loadLibrary("masque_core") }
    }
    private external fun nativeOpen(configuration: ByteArray): ByteArray
    private external fun nativeCall(handle: Long, command: ByteArray): ByteArray
    private external fun nativeClose(handle: Long)

    private fun decode(text: String): JsonObject {
        val value = Json.parseToJsonElement(text).jsonObject
        value["error"]?.let {
            value["code"]?.let { code -> throw AGWBindingException(code.jsonPrimitive.content, it.jsonPrimitive.content) }
            throw IllegalStateException(it.jsonPrimitive.content)
        }
        return value
    }
    override suspend fun open(configuration: JsonObject): Long = withContext(Dispatchers.IO) {
        loading.getOrThrow()
        decode(nativeOpen(configuration.toString().toByteArray(Charsets.UTF_8)).toString(Charsets.UTF_8)).getValue("handle").jsonPrimitive.long
    }
    override suspend fun call(handle: Long, command: JsonObject): JsonObject = withContext(Dispatchers.IO) {
        decode(nativeCall(handle, command.toString().toByteArray(Charsets.UTF_8)).toString(Charsets.UTF_8))
    }
    override fun close(handle: Long) { nativeClose(handle) }
}
