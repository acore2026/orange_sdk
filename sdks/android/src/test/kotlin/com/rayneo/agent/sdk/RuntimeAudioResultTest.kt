package com.rayneo.agent.sdk

import com.rayneo.agent.sdk.model.parseRuntimeAudioResult
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test

class RuntimeAudioResultTest {
    private val response = Json.parseToJsonElement("""{"request_id":"asr-action-001","text":"Expel the suspect.","intent":{"executor":"robot dog","type":"movement","direction":"expel","matched":true,"backend":"qwen"}}""").jsonObject
    @Test fun parsesDocumentedEnglishResponse() {
        val result = parseRuntimeAudioResult(response, "asr-action-001")
        assertEquals("expel", result.direction)
        assertEquals("Expel the suspect.", result.text)
        assertTrue(result.matched)
    }
    @Test fun rejectsWrongRequestAndNonBooleanMatch() {
        assertThrows(IllegalArgumentException::class.java) { parseRuntimeAudioResult(response, "wrong") }
        val malformed = Json.parseToJsonElement(response.toString().replace("true", "\"true\"")).jsonObject
        assertThrows(IllegalArgumentException::class.java) { parseRuntimeAudioResult(malformed, "asr-action-001") }
    }
}
