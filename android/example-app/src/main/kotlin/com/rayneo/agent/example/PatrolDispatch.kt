package com.rayneo.agent.example

import java.util.UUID
import kotlinx.coroutines.CompletableDeferred
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive

/** One user-confirmed patrol. Retain this object across transport retries. */
internal class PatrolDispatch(
    val groupId: String,
    val agentId: String,
    val taskId: String = "patrol-${UUID.randomUUID()}",
) {
    val acceptance = CompletableDeferred<String?>()
    val entryCompletion = CompletableDeferred<String?>()

    fun receive(group: String, sender: String, payload: JsonObject): Boolean {
        if (group != groupId || sender != agentId ||
            payload["task_id"]?.jsonPrimitive?.contentOrNull != taskId) return false
        val event = payload["type"]?.jsonPrimitive?.contentOrNull
        when (event) {
            "patrol_accepted" -> {
                val duplicate = payload["duplicate"]?.jsonPrimitive?.booleanOrNull == true ||
                    payload["execution"]?.jsonPrimitive?.contentOrNull == "already_received_no_replay"
                acceptance.complete(if (duplicate)
                    "机器狗判定为重复任务，未重新执行；请发起新一轮巡逻 / Duplicate task; no replay"
                    else null)
            }
            "patrol_rejected", "patrol_confirmation_required" -> acceptance.complete(
                "机器狗未接受巡逻：${payload["reason"]?.jsonPrimitive?.contentOrNull ?: event}",
            )
            "dog_motion_completed", "dog_motion_failed" -> {
                // Ordinary remote-control actions can share a patrol context. They must
                // never satisfy the entry-motion acknowledgement.
                if (payload["command"]?.jsonPrimitive?.contentOrNull != "patrol_forward") return false
                entryCompletion.complete(if (event == "dog_motion_failed")
                    payload["reason"]?.jsonPrimitive?.contentOrNull ?: "巡逻起步失败"
                    else null)
            }
            else -> return false
        }
        return true
    }

    fun close() {
        acceptance.cancel()
        entryCompletion.cancel()
    }
}
