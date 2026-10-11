package com.rayneo.agent.sdk.model

import kotlinx.serialization.json.*

data class RuntimeAudioResult(
    val requestId: String,
    val text: String,
    val executor: String?,
    val type: String,
    val direction: String?,
    val matched: Boolean,
    val backend: String,
)

/** Reject malformed or uncorrelated responses before any device action is considered. */
fun parseRuntimeAudioResult(body: JsonObject, expectedRequestId: String): RuntimeAudioResult {
    fun JsonObject.string(key: String): String =
        (get(key) as? JsonPrimitive)?.takeIf { it.isString }?.content
            ?: throw IllegalArgumentException("Runtime ASR $key must be a string")
    val id = body.string("request_id")
    require(id == expectedRequestId) { "Runtime ASR request_id mismatch" }
    val intent = body["intent"] as? JsonObject
        ?: throw IllegalArgumentException("Runtime ASR intent must be an object")
    val matched = (intent["matched"] as? JsonPrimitive)?.takeIf { !it.isString }?.booleanOrNull
        ?: throw IllegalArgumentException("Runtime ASR matched must be a boolean")
    val direction = intent["direction"]?.takeUnless { it is JsonNull }?.let {
        (it as? JsonPrimitive)?.takeIf { value -> value.isString }?.content
            ?: throw IllegalArgumentException("Runtime ASR direction must be a string")
    }
    return RuntimeAudioResult(id, body.string("text"), intent["executor"]?.takeUnless { it is JsonNull }?.let { intent.string("executor") },
        intent.string("type"), direction, matched, intent.string("backend"))
}
