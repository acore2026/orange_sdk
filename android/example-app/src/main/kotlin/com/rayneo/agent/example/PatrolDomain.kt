package com.rayneo.agent.example

/** Pure domain model for an AR park patrol. No Android or coroutine dependencies. */
enum class PatrolPhase {
    IDLE,
    LISTENING,
    PLANNING,
    DISPATCHING,
    NAVIGATING,
    INSPECTING,
    HAZARD_RESPONSE,
    REPORTING,
    COMPLETED,
    ABORTED,
}

enum class PatrolIntent {
    SECURITY_PATROL,
    INSPECT_ZONE,
    REPORT_HAZARD,
    /** Semantic aliases used by the park command vocabulary. */
    THREATEN,
    EVICT,
    STOP_PATROL,
    RESUME_PATROL,
    UNKNOWN,
}

enum class AgentCapability {
    PATROL,
    SCRAPE,
    FRONT_POUNCE,
    HAZARD_RESPONSE,
    REPORT,
}

enum class AgentAvailability { AVAILABLE, BUSY, OFFLINE }

data class CandidateAgent(
    val id: String,
    val name: String,
    val capabilities: Set<AgentCapability> = emptySet(),
    val availability: AgentAvailability = AgentAvailability.AVAILABLE,
    val distanceMeters: Double? = null,
)

enum class HazardSeverity { LOW, MEDIUM, HIGH, CRITICAL }
enum class HazardStatus { OPEN, ACKNOWLEDGED, MITIGATED, DISMISSED }

data class HazardAlert(
    val id: String,
    val zoneId: String,
    val severity: HazardSeverity,
    val description: String,
    val status: HazardStatus = HazardStatus.OPEN,
)

enum class PatrolActionType { SCRAPE, FRONT_POUNCE }

/** Stable action names shared by the voice UI and the robot A2A transport. */
enum class RobotAction {
    FORWARD,
    BACKWARD,
    LEFT,
    RIGHT,
    STOP,
    SCRAPE,
    FRONT_POUNCE,
}

/** The two execution actions used by patrol agents. */
sealed class PatrolAction {
    abstract val targetZoneId: String

    data class Scrape(
        override val targetZoneId: String,
        val reason: String = "routine inspection",
    ) : PatrolAction()

    data class FrontPounce(
        override val targetZoneId: String,
        val hazardId: String? = null,
        val reason: String = "hazard response",
    ) : PatrolAction()
}

data class PatrolRequest(
    val zoneId: String,
    val intent: PatrolIntent = PatrolIntent.SECURITY_PATROL,
    val requestedBy: String = "user",
    val hazardId: String? = null,
)

data class PatrolState(
    val phase: PatrolPhase = PatrolPhase.IDLE,
    val request: PatrolRequest? = null,
    val selectedAgent: CandidateAgent? = null,
    val candidates: List<CandidateAgent> = emptyList(),
    val activeAction: PatrolAction? = null,
    val alerts: List<HazardAlert> = emptyList(),
    val failureReason: String? = null,
)

// Domain vocabulary aliases kept source-compatible for UI and integration code.
typealias PatrolAgentCandidate = CandidateAgent
typealias DangerAlert = HazardAlert
typealias PatrolUiState = PatrolState

sealed class PatrolEvent {
    data class ReceiveRequest(val request: PatrolRequest) : PatrolEvent()
    data class CandidatesDiscovered(val agents: List<CandidateAgent>) : PatrolEvent()
    data class AgentSelected(val agent: CandidateAgent) : PatrolEvent()
    data object NavigationStarted : PatrolEvent()
    data object InspectionStarted : PatrolEvent()
    data class HazardDetected(val alert: HazardAlert) : PatrolEvent()
    data class HazardResolved(val alertId: String) : PatrolEvent()
    data object ReportSubmitted : PatrolEvent()
    data object Stop : PatrolEvent()
    data class Fail(val reason: String) : PatrolEvent()
    data object Reset : PatrolEvent()
}

data class PatrolTransition(
    val state: PatrolState,
    val accepted: Boolean,
    val reason: String? = null,
)

/** Maps user intent and hazard context to the concrete patrol action. */
fun mapPatrolAction(request: PatrolRequest, alert: HazardAlert? = null): PatrolAction? = when {
    request.intent == PatrolIntent.STOP_PATROL || request.intent == PatrolIntent.UNKNOWN -> null
    alert != null && alert.severity >= HazardSeverity.HIGH ->
        PatrolAction.FrontPounce(request.zoneId, alert.id, alert.description)
    request.intent == PatrolIntent.REPORT_HAZARD || request.intent == PatrolIntent.EVICT ->
        PatrolAction.FrontPounce(request.zoneId, request.hazardId, "reported hazard")
    else -> PatrolAction.Scrape(request.zoneId)
}

/** Maps command vocabulary to the compact robot action enum. */
fun mapRobotAction(intent: PatrolIntent, alert: HazardAlert? = null): RobotAction? = when {
    intent == PatrolIntent.STOP_PATROL || intent == PatrolIntent.UNKNOWN -> null
    intent == PatrolIntent.EVICT || intent == PatrolIntent.REPORT_HAZARD ||
        (alert != null && alert.severity >= HazardSeverity.HIGH) -> RobotAction.FRONT_POUNCE
    // THREATEN is a visual evidence collection command in the patrol vocabulary.
    intent == PatrolIntent.THREATEN || intent == PatrolIntent.SECURITY_PATROL ||
        intent == PatrolIntent.INSPECT_ZONE || intent == PatrolIntent.RESUME_PATROL -> RobotAction.SCRAPE
    else -> null
}

object PatrolActionMapping {
    fun forIntent(request: PatrolRequest, alert: HazardAlert? = null): PatrolAction? =
        mapPatrolAction(request, alert)

    fun typeOf(action: PatrolAction): PatrolActionType = when (action) {
        is PatrolAction.Scrape -> PatrolActionType.SCRAPE
        is PatrolAction.FrontPounce -> PatrolActionType.FRONT_POUNCE
    }
}

fun validatePatrolRequest(request: PatrolRequest): List<String> = buildList {
    if (request.zoneId.isBlank()) add("zoneId must not be blank")
    if (request.requestedBy.isBlank()) add("requestedBy must not be blank")
    if (request.intent == PatrolIntent.UNKNOWN) add("intent must be recognized")
}

fun validateCandidate(agent: CandidateAgent): List<String> = buildList {
    if (agent.id.isBlank()) add("agent id must not be blank")
    if (agent.name.isBlank()) add("agent name must not be blank")
    if (agent.distanceMeters != null && agent.distanceMeters < 0.0) {
        add("distanceMeters must be non-negative")
    }
}

fun validateHazard(alert: HazardAlert): List<String> = buildList {
    if (alert.id.isBlank()) add("hazard id must not be blank")
    if (alert.zoneId.isBlank()) add("hazard zoneId must not be blank")
    if (alert.description.isBlank()) add("hazard description must not be blank")
}

fun chooseCandidate(candidates: List<CandidateAgent>, required: Set<AgentCapability>): CandidateAgent? =
    candidates.asSequence()
        .filter { it.availability == AgentAvailability.AVAILABLE }
        .filter { it.capabilities.containsAll(required) }
        .sortedWith(compareBy<CandidateAgent> { it.distanceMeters ?: Double.MAX_VALUE }.thenBy { it.id })
        .firstOrNull()

fun parsePatrolConfirmation(spoken: String): Boolean? {
    val command = spoken.trim()
    return when {
        listOf("取消", "不确认", "不用", "不要派遣").any(command::contains) -> false
        listOf("确认", "同意", "派遣").any(command::contains) -> true
        else -> null
    }
}

/** Deterministic, immutable state transition. Invalid events leave the state unchanged. */
fun transitionPatrol(state: PatrolState, event: PatrolEvent): PatrolTransition {
    fun reject(message: String) = PatrolTransition(state, accepted = false, reason = message)
    return when (event) {
        is PatrolEvent.ReceiveRequest -> if (state.phase in setOf(PatrolPhase.IDLE, PatrolPhase.COMPLETED, PatrolPhase.ABORTED)) {
            val errors = validatePatrolRequest(event.request)
            if (errors.isNotEmpty()) reject(errors.joinToString("; "))
            else PatrolTransition(state.copy(phase = PatrolPhase.PLANNING, request = event.request, failureReason = null), true)
        } else reject("cannot receive a request while ${state.phase}")
        is PatrolEvent.CandidatesDiscovered -> if (state.phase == PatrolPhase.PLANNING) {
            PatrolTransition(state.copy(phase = PatrolPhase.DISPATCHING, candidates = event.agents), true)
        } else reject("candidates require PLANNING phase")
        is PatrolEvent.AgentSelected -> if (state.phase == PatrolPhase.DISPATCHING &&
            event.agent.availability == AgentAvailability.AVAILABLE) {
            PatrolTransition(state.copy(phase = PatrolPhase.NAVIGATING, selectedAgent = event.agent), true)
        } else reject("agent selection requires an available agent in DISPATCHING phase")
        PatrolEvent.NavigationStarted -> if (state.phase == PatrolPhase.NAVIGATING) {
            PatrolTransition(state.copy(phase = PatrolPhase.INSPECTING), true)
        } else reject("navigation can start only from NAVIGATING phase")
        PatrolEvent.InspectionStarted -> if (state.phase == PatrolPhase.NAVIGATING) {
            PatrolTransition(state.copy(phase = PatrolPhase.INSPECTING), true)
        } else reject("inspection can start only from NAVIGATING phase")
        is PatrolEvent.HazardDetected -> if (state.phase == PatrolPhase.INSPECTING || state.phase == PatrolPhase.NAVIGATING) {
            val action = mapPatrolAction(state.request ?: return reject("hazard requires an active request"), event.alert)
            PatrolTransition(state.copy(phase = PatrolPhase.HAZARD_RESPONSE, alerts = state.alerts + event.alert, activeAction = action), true)
        } else reject("hazard can be detected during navigation or inspection")
        is PatrolEvent.HazardResolved -> if (state.phase == PatrolPhase.HAZARD_RESPONSE && state.alerts.any { it.id == event.alertId }) {
            val updated = state.alerts.map { if (it.id == event.alertId) it.copy(status = HazardStatus.MITIGATED) else it }
            PatrolTransition(state.copy(phase = PatrolPhase.REPORTING, alerts = updated), true)
        } else reject("unknown hazard or no active hazard response")
        PatrolEvent.ReportSubmitted -> if (state.phase == PatrolPhase.REPORTING || state.phase == PatrolPhase.INSPECTING) {
            PatrolTransition(state.copy(phase = PatrolPhase.COMPLETED), true)
        } else reject("report can be submitted only after inspection")
        PatrolEvent.Stop -> PatrolTransition(state.copy(phase = PatrolPhase.ABORTED, activeAction = null), true)
        is PatrolEvent.Fail -> PatrolTransition(state.copy(phase = PatrolPhase.ABORTED, failureReason = event.reason, activeAction = null), true)
        PatrolEvent.Reset -> PatrolTransition(PatrolState(), true)
    }
}
