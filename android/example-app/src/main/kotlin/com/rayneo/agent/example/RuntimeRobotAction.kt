package com.rayneo.agent.example

import com.rayneo.agent.sdk.model.RuntimeAudioResult

/** Transcript text is display-only: actions come exclusively from the structured intent. */
fun runtimeRobotAction(result: RuntimeAudioResult): RobotAction? {
    if (!result.matched || result.executor != "robot dog" || result.type != "movement") return null
    return when (result.direction) {
        "forward" -> RobotAction.FORWARD
        "backward" -> RobotAction.BACKWARD
        "left" -> RobotAction.LEFT
        "right" -> RobotAction.RIGHT
        "stop" -> RobotAction.STOP
        "wave" -> RobotAction.WAVE
        "deter" -> RobotAction.SCRAPE
        "expel" -> RobotAction.FRONT_POUNCE
        else -> null
    }
}
