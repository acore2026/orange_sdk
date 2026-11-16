package com.rayneo.agent.example

/** Human-facing AR summary. Transport errors and machine identifiers stay in diagnostics. */
internal data class PatrolPresentation(val title: String, val summary: String)

internal fun patrolPresentation(
    state: PatrolState,
    identityReady: Boolean,
    connected: Boolean,
    videoReady: Boolean,
    hasFrame: Boolean,
    stalled: Boolean,
): PatrolPresentation {
    val candidate = state.selectedAgent ?: state.candidates.firstOrNull()
    val name = candidate?.name?.trim()?.takeIf {
        it.isNotEmpty() && it != candidate.id && !it.contains(Regex("[0-9a-fA-F]{16,}|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}|did:|https?://"))
    }?.takeIf { name -> name.none { it in '\u4e00'..'\u9fff' } }?.take(32) ?: "Patrol Robot Dog"
    val capabilities = candidate?.capabilities.orEmpty()
    val introduction = listOfNotNull(
        "Autonomous patrol".takeIf { AgentCapability.PATROL in capabilities },
        "On-site response".takeIf { AgentCapability.HAZARD_RESPONSE in capabilities },
        "Patrol reports".takeIf { AgentCapability.REPORT in capabilities },
    ).joinToString(" · ").ifBlank { "Quadruped robot · Field partner" }
    val title = when {
        stalled -> "Video temporarily interrupted"
        state.failureReason != null -> "Task incomplete"
        connected && !videoReady -> "Connecting field video"
        videoReady && !hasFrame -> "Waiting for live video"
        else -> when (state.phase) {
            PatrolPhase.IDLE -> if (identityReady) "Digital identity ready" else "Establishing digital identity"
            PatrolPhase.LISTENING -> "Awaiting patrol task"
            PatrolPhase.PLANNING -> "Finding a patrol partner"
            PatrolPhase.DISPATCHING -> "Robot dog found · Confirm dispatch"
            PatrolPhase.NAVIGATING -> "Establishing collaboration network"
            PatrolPhase.INSPECTING -> "Robot dog on patrol"
            PatrolPhase.HAZARD_RESPONSE -> "Responding to an incident"
            PatrolPhase.REPORTING -> "Preparing patrol report"
            PatrolPhase.COMPLETED -> "Patrol complete"
            PatrolPhase.ABORTED -> "Task cancelled"
        }
    }
    val summary = when {
        candidate != null -> "$name\n$introduction"
        connected -> "Patrol Robot Dog · Collaboration network joined"
        identityReady -> "RayNeo AR Glasses · Patrol command terminal\nReady for your patrol command"
        else -> "RayNeo AR Glasses · Patrol command terminal\nPreparing secure collaboration identity"
    }
    return PatrolPresentation(title, summary)
}
