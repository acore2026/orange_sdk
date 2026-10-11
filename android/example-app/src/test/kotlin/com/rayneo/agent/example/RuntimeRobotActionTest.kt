package com.rayneo.agent.example

import com.rayneo.agent.sdk.model.RuntimeAudioResult
import org.junit.Assert.*
import org.junit.Test

class RuntimeRobotActionTest {
    private val expel = RuntimeAudioResult("r", "Expel the suspect.", "robot dog", "movement", "expel", true, "qwen")

    @Test fun englishConfirmationAndCancellation() {
        assertEquals(true, parsePatrolConfirmation("Confirm."))
        assertEquals(true, parsePatrolConfirmation("YES"))
        assertEquals(false, parsePatrolConfirmation("Cancel!"))
        assertEquals(false, parsePatrolConfirmation("Do not confirm"))
        assertNull(parsePatrolConfirmation("Do not expel"))
    }

    @Test fun englishDirectionsUseStructuredIntent() {
        val expected = mapOf("forward" to RobotAction.FORWARD, "backward" to RobotAction.BACKWARD,
            "left" to RobotAction.LEFT, "right" to RobotAction.RIGHT, "wave" to RobotAction.WAVE, "deter" to RobotAction.SCRAPE,
            "expel" to RobotAction.FRONT_POUNCE)
        expected.forEach { (direction, action) ->
            assertEquals(action, runtimeRobotAction(expel.copy(direction = direction)))
        }
        assertEquals(RobotAction.FRONT_POUNCE, runtimeRobotAction(expel.copy(text = "驱逐")))
    }

    @Test fun rejectedIntentNeverFallsBackToTranscript() {
        assertNull(runtimeRobotAction(expel.copy(matched = false)))
        assertNull(runtimeRobotAction(expel.copy(type = "other")))
        assertNull(runtimeRobotAction(expel.copy(executor = "other")))
        assertNull(runtimeRobotAction(expel.copy(direction = "unknown")))
        assertNull(runtimeRobotAction(expel.copy(direction = null)))
    }
}
